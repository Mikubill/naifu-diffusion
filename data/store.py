import json
import os
import random
import torch
import h5py
import os, binascii
import numpy as np

from pathlib import Path
from PIL import Image
from torchvision import transforms
from tqdm.auto import tqdm
from lightning.pytorch.utilities import rank_zero_only
from data.buckets import AspectRatioBucket

def get_class(name: str):
    import importlib
    module_name, class_name = name.rsplit(".", 1)
    module = importlib.import_module(module_name, package=None)
    return getattr(module, class_name)


class ImageStore(torch.utils.data.IterableDataset):
    """
    A dataset to prepare the instance and class images with the prompts for fine-tuning the model.
    It pre-processes the images and the tokenizes prompts.
    """

    def __init__(
        self,
        img_path,
        size=512,
        center_crop=False,
        ucg=0,
        rank=0,
        tag_processor=[],
        world_size=1,
        important_tags=[],
        allow_duplicates=False,
        **kwargs
    ):
        self.size = size
        self.center_crop = center_crop
        self.ucg = ucg
        self.rank = rank
        self.world_size = world_size
        self.dataset = img_path
        self.allow_duplicates = allow_duplicates
        self.important_tags = important_tags
        self.image_transforms = transforms.Compose(
            [
                transforms.Resize(size, interpolation=transforms.InterpolationMode.LANCZOS),
                transforms.CenterCrop(size) if center_crop else transforms.RandomCrop(size),
                transforms.ToTensor(),
                transforms.Normalize([0.5], [0.5]),
            ]
        )
        
        if isinstance(self.dataset, str):
            self.dataset = [self.dataset]
            
        self.tag_processor = []
        for processor in tag_processor:
            if processor != "":
                self.tag_processor.append(get_class(processor))
        
        self.latent_cache = {}
        self.update_store()

    def prompt_resolver(self, x: str):
        img_item = (x, "")
        fp = os.path.splitext(x)[0]

        with open(fp + ".txt") as f:
            content = f.read()
            new_prompt = content

        return str(x), new_prompt

    def update_store(self):   
        self.entries = []
        bar = tqdm(total=-1, desc=f"Loading captions", disable=self.rank not in [0, -1])
        folders = []
        for entry in self.dataset:
            if self.allow_duplicates and not isinstance(entry, str):
                folders.extend([entry[0] for _ in range(entry[1])])
            else:
                folders.append(entry)
        
        for entry in folders:            
            for x in Path(entry).rglob("*"):
                if not (x.is_file() and x.suffix in [".jpg", ".png", ".webp", ".bmp", ".gif", ".jpeg", ".tiff"]):
                    continue
                img, prompt = self.prompt_resolver(x)
                _, skip = self.process_tags(prompt)
                if skip:
                    continue
                
                if self.allow_duplicates:
                    prefix = binascii.hexlify(os.urandom(5))
                    img = f"{prefix}@{img}"
                
                self.entries.append((img, prompt))
                bar.update(1)

        self._length = len(self.entries)
        random.shuffle(self.entries)

    def read_img(self, filepath):  
        if self.allow_duplicates and "@" in filepath:
            filepath = filepath[filepath.index("@")+1:]    
        img = Image.open(filepath)
        if not img.mode == "RGB":
            img = img.convert("RGB")
        return img
    
    def process_tags(self, tags):
        if len(self.tag_processor) == 0:
            return tags, False
        
        reject = False
        for processor in self.tag_processor:
            tags, rej_cur = processor(tags)
            reject = reject or rej_cur
            
        return tags, reject
    
    # cc: openai
    def crop_rectangle(self, arrays):
        min_width = np.min([array.shape[2] for array in arrays])
        min_height = np.min([array.shape[1] for array in arrays])

        start_width = []
        start_height = []

        for array in arrays:
            width_diff = array.shape[2] - min_width
            height_diff = array.shape[1] - min_height
            start_width.append(width_diff // 2)
            start_height.append(height_diff // 2)

        end_width = [start + min_width for start in start_width]
        end_height = [start + min_height for start in start_height]
        
        cropped_arrays = [
            array[:, start_height[i]:end_height[i], start_width[i]:end_width[i]]
            for i, array in enumerate(arrays)
        ]
        return cropped_arrays

    def collate_fn(self, examples):
        result = {}
        if "latents" in examples[0].keys():
            latents = torch.stack(self.crop_rectangle([example["latents"] for example in examples]))
            result.update({"latents": latents})
        else:
            pixel_values = [example["images"] for example in examples]
            pixel_values = torch.stack(pixel_values).to(memory_format=torch.contiguous_format).float()
            result.update({"images": pixel_values})
        
        if "conds" in examples[0].keys():
            conds = {}
            for example in examples:
                # conds is a dict, concat all values by keys
                for key in example["conds"]:
                    if key not in conds:
                        conds[key] = []
                    conds[key].append(example["conds"][key])
            for key in conds:
                conds[key] = torch.stack(conds[key])
            result.update({"conds": conds})
        else:
            result.update({
                "prompts": [example["prompts"] for example in examples],
                "target_size_as_tuple": torch.stack([example["target_size_as_tuple"] for example in examples]),
                "original_size_as_tuple": torch.stack([example["original_size_as_tuple"] for example in examples]),
                "crop_coords_top_left": torch.stack([example["crop_coords_top_left"] for example in examples]),
            })
        return result

    def __len__(self):
        return self._length // self.world_size

    def __iter__(self):
        for entry in self.entries[self.rank::self.world_size]:
            instance_path, instance_prompt = entry
            
            instance_image = self.read_img(instance_path)
            img = self.image_transforms(instance_image)
            prompts = instance_prompt
            
            inst = {
                "images": img, 
                "prompts": prompts,
                "original_size_as_tuple": (self.size, self.size),
                "crop_coords_top_left": (0,0),
                "target_size_as_tuple": (self.size, self.size),
            }
            return inst
    
    
class AspectRatioDataset(ImageStore):
    def __init__(self, arb_config, debug_arb=False, **kwargs):
        super().__init__(**kwargs)
        self.debug = debug_arb
        self.prompt_cache = {}
        self.kwargs = kwargs
        self.cache_enabled = kwargs.get("enabled", False)
        self.cache_dir = kwargs.get("cache_dir", "cache")
        self.cache_bsz = kwargs.get("cache_bsz", 4)
        self.cache_target = kwargs.get("target", []) if self.cache_enabled else []
        
        for path, prompt in self.entries:
            self.prompt_cache[path] = prompt
            
        self.arb_config = arb_config
        if arb_config["debug"]:
            print(f"BucketManager initialized using config: {self.arb_config}")
        else:
            print(f"BucketManager initialized with base_res = {self.arb_config['base_res']}, max_size = {self.arb_config['max_size']}")
        
        for path, prompt in self.entries:
            self.prompt_cache[path] = prompt
        
        self.init_buckets()

    def init_buckets(self):
        entries = [x[0] for x in self.entries]
        self.buckets = AspectRatioBucket(self.get_dict(entries), **self.arb_config)
        
    def get_dict(self, entries):
        id_size_map = {}
        for entry in tqdm(iter(entries), desc=f"Loading resolutions", disable=self.rank not in [0, -1]):
            fp = entry[entry.index("@")+1:] if self.kwargs.get("allow_duplicates") else entry
            with Image.open(fp) as img:
                size = img.size
            id_size_map[entry] = size
        return id_size_map
        
    def denormalize(self, img, mean=0.5, std=0.5):
        res = transforms.Normalize((-1*mean/std), (1.0/std))(img.squeeze(0))
        res = torch.clamp(res, 0, 1)
        return res
    
    def transformer(self, img, size, center_crop=False):
        x, y = img.size
        short, long = (x, y) if x <= y else (y, x)

        w, h = size
        min_crop, max_crop = (w, h) if w <= h else (h, w)
        ratio_src, ratio_dst = float(long / short), float(max_crop / min_crop)

        if ratio_src > ratio_dst:
            new_w, new_h = (min_crop, int(min_crop * ratio_src)) if x < y else (int(min_crop * ratio_src), min_crop)
        elif ratio_src < ratio_dst:
            new_w, new_h = (max_crop, int(max_crop / ratio_src)) if x > y else (int(max_crop / ratio_src), max_crop)
        else:
            new_w, new_h = w, h

        image_transforms = transforms.Compose([
            transforms.Resize((new_h, new_w), interpolation=transforms.InterpolationMode.LANCZOS),
            transforms.CenterCrop((h, w)) if center_crop else transforms.RandomCrop((h, w)),
            transforms.ToTensor(),
            transforms.Normalize([0.5], [0.5])
        ])

        new_img = image_transforms(img)

        return new_img
    
    @rank_zero_only
    def setup_cache(self, vae_encode_func, token_encode_func):
        cache_dir = Path(self.cache_dir)
        store = self.buckets
        self.cache_images = "images" in self.cache_target
        self.cache_prompts = "prompts" in self.cache_target
        if not cache_dir.exists():
            cache_dir.mkdir(parents=True, exist_ok=True)

        cache_file = cache_dir / "cache.h5"
        if cache_file.exists():
            
            with h5py.File(cache_file, "r") as cache:
                to_cache_images = self.cache_images and any(f"{img}.latents" not in cache for entry in store.buckets.keys() for img in store.buckets[entry][:])
                to_cache_prompts = self.cache_prompts and any(f"{img}.crossattn" not in cache for entry in store.buckets.keys() for img in store.buckets[entry][:])
                if not to_cache_images and not to_cache_prompts:
                    print(f"Restored cache from {cache_file.absolute()}")
                    return
            
        with h5py.File(cache_file, "r+") if cache_file.exists() else h5py.File(cache_file, "w") as cache:
            self.fulfill_cache(cache, vae_encode_func, token_encode_func, store)

    def fulfill_cache(self, cache, vae_encode_func, token_encode_func, store):
        progress_bar = tqdm(total=len(self.entries), desc=f"Caching", disable=self.rank not in [0, -1])
        for entry in store.buckets.keys():
            size = store.resolutions[entry]
            imgs = store.buckets[entry][:]
            stride = self.cache_bsz
            for idx in range(0, len(imgs), stride):                
   
                if self.cache_images and not all([f"{img}.latents" in cache for img in imgs[idx:idx+stride]]):
                    batch = []
                    for img in imgs[idx:idx+stride]:
                        img_data = self.read_img(img)
                        batch.append(self.transformer(img_data, size, center_crop=True))
                        
                    latent = vae_encode_func(torch.stack(batch).cuda())
                    for img, latent in zip(imgs[idx:idx+stride], latent):
                        img = str(img)
                        cache.create_dataset(f"{img}.latents", data=latent.detach().squeeze(0).half().cpu().numpy())
                        cache.create_dataset(f"{img}.size", data=size)
                        
                if self.cache_prompts and not all([f"{img}.crossattn" in cache for img in imgs[idx:idx+stride]]):
                    prompt_batch = []
                    for img in imgs[idx:idx+stride]:
                        prompt_data = self.prompt_cache[img]
                        prompt_batch.append(prompt_data)
                    prompt = {
                        "prompts": prompt_batch,
                        "original_size_as_tuple": torch.stack([torch.asarray(size) for _ in prompt_batch]).cuda(),
                        "crop_coords_top_left": torch.stack([torch.asarray((0,0)) for _ in prompt_batch]).cuda(),
                        "target_size_as_tuple": torch.stack([torch.asarray(size) for _ in prompt_batch]).cuda(), 
                    }
                    conds = token_encode_func(prompt)
                    conds, vectors = conds["crossattn"], conds.get("vector", None)
                    for idx, img in enumerate(imgs[idx:idx+stride]):
                        img = str(img)
                        cond = conds[idx, ...]
                        cache.create_dataset(f"{img}.crossattn", data=cond.detach().half().cpu().numpy())
                        
                        if vectors is not None:
                            vec = vectors[idx, ...]
                            cache.create_dataset(f"{img}.vec", data=vec.detach().half().cpu().numpy())

                progress_bar.update(len(imgs[idx:idx+stride]))
        progress_bar.close()

    def build_dict(self, item) -> dict:
        item_id, size, = item["instance"], item["size"],
        cache_images = "images" in self.cache_target
        cache_prompts = "prompts" in self.cache_target
        
        result = {}
        if item_id == "":
            return {}
        
        if self.cache_enabled:
            with h5py.File(Path(self.cache_dir) / "cache.h5") as cache:
                if cache_images:
                    latent = torch.asarray(cache[f"{item_id}.latents"][:])
                    latent_size = cache[f"{item_id}.size"][:]
                    estimate_size = latent_size[1] // 8, latent_size[0] // 8,
                    if latent.shape != (4, *estimate_size):
                        print(f"Latent shape mismatch for {item_id}! Expected {estimate_size}, got {latent.shape}") 
                    result.update({"latents": latent})
                if cache_prompts:
                    prompt = {"crossattn": torch.asarray(cache[f"{item_id}.crossattn"][:])}   
                    if f"{item_id}.vec" in cache:
                        prompt.update({"vector": torch.asarray(cache[f"{item_id}.vec"][:])}) 
                    result.update({"conds": prompt})
                if len(result) == 2:
                    return result

        if not cache_prompts:
            prompt, _ = self.process_tags(self.prompt_cache[item_id])
            if random.random() < self.ucg:
                prompt = ''
                
            w, h = size
            result.update({
                "prompts": prompt,
                "original_size_as_tuple": torch.asarray((h, w)),
                "crop_coords_top_left": torch.asarray((0,0)),
                "target_size_as_tuple": torch.asarray((h, w)),
            })
            
        if not cache_images:
            image = self.read_img(item_id)
            image = self.transformer(image, size)
            result.update({"images": image})
        return result
    
    def __len__(self):
        return len(self.buckets.res_map) // self.world_size

    def __iter__(self):
        for batch, size in self.buckets.generator():
            for item in batch:
                yield self.build_dict({"size": size, "instance": item})

    # def __getitem__(self, index):
    #     return [self.build_dict(item) for item in index]
