"""Build diffusers / transformers models from single standard .safetensors files (or HF folders).

Runs only inside the engine worker process (imports torch). No network access:
model configs are either diffusers defaults or derived from the weight shapes.
"""

import glob
import json
import os

import torch
from accelerate import init_empty_weights
from safetensors.torch import load_file

ASSETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")

# Qwen3-style chat template (no default system prompt), used by Z-Image's text encoder.
QWEN3_CHAT_TEMPLATE = (
    "{% for m in messages %}<|im_start|>{{ m['role'] }}\n{{ m['content'] }}<|im_end|>\n{% endfor %}"
    "{% if add_generation_prompt %}<|im_start|>assistant\n{% endif %}"
)


def _instantiate(cls, state_dict, dtype, **config):
    """Create a model without allocating weights, then adopt the tensors from the state dict."""
    with init_empty_weights():
        model = cls(**config)
    expected = set(model.state_dict())
    state_dict = {k: v for k, v in state_dict.items() if k in expected}
    missing = expected - set(state_dict)
    if missing:
        raise ValueError(f"{cls.__name__}: {len(missing)} weights missing, e.g. {sorted(missing)[:3]}")
    model.load_state_dict(state_dict, assign=True, strict=True)
    if dtype is not None:
        model.to(dtype)
    return model.eval()


def _cast_small_params(model, dtype):
    """Keep big Linear weights as stored (e.g. fp8) but make norms/biases/embeddings the compute dtype."""
    for module in model.modules():
        for name, p in list(module.named_parameters(recurse=False)):
            if isinstance(module, torch.nn.Linear) and name == "weight":
                continue
            if p.dtype != dtype:
                setattr(module, name, torch.nn.Parameter(p.data.to(dtype), requires_grad=False))


def load_transformer(family, path):
    sd = load_file(path)
    fp8 = any(v.dtype in (torch.float8_e4m3fn, torch.float8_e5m2) for v in sd.values())
    if family == "z-image":
        from diffusers import ZImageTransformer2DModel
        from diffusers.loaders.single_file_utils import convert_z_image_transformer_checkpoint_to_diffusers
        sd = convert_z_image_transformer_checkpoint_to_diffusers(sd)
        model = _instantiate(ZImageTransformer2DModel, sd, None if fp8 else torch.bfloat16)
    elif family == "qwen-image":
        from diffusers import QwenImageTransformer2DModel
        model = _instantiate(QwenImageTransformer2DModel, sd, None if fp8 else torch.bfloat16)
    else:
        raise ValueError(f"No loader for diffusion family '{family}' yet")
    if fp8:
        # Keep Linear weights in fp8 (half the memory of bf16); each layer upcasts its weight when it runs.
        _cast_small_params(model, torch.bfloat16)
        _enable_fp8_linears(model)
    return model


def _fp8_linear_forward(self, x):
    return torch.nn.functional.linear(x, self.weight.to(x.dtype), self.bias)


def _enable_fp8_linears(model):
    import types
    for module in model.modules():
        if isinstance(module, torch.nn.Linear) and module.weight.element_size() == 1:
            module.forward = types.MethodType(_fp8_linear_forward, module)
            # PEFT reads this to create LoRA weights in bf16 instead of the base weight's fp8.
            module.compute_dtype = torch.bfloat16


def load_vae(family, path):
    sd = load_file(path)
    if family == "wan2.1-vae":
        from diffusers import AutoencoderKLQwenImage
        from diffusers.loaders.single_file_utils import convert_wan_vae_to_diffusers
        return _instantiate(AutoencoderKLQwenImage, convert_wan_vae_to_diffusers(sd), torch.bfloat16)
    if family == "flux-vae":
        from diffusers import AutoencoderKL
        from diffusers.loaders.single_file_utils import convert_ldm_vae_checkpoint
        cfg = dict(in_channels=3, out_channels=3, latent_channels=16, block_out_channels=(128, 256, 512, 512),
                   down_block_types=("DownEncoderBlock2D",) * 4, up_block_types=("UpDecoderBlock2D",) * 4,
                   layers_per_block=2, norm_num_groups=32, use_quant_conv=False, use_post_quant_conv=False,
                   scaling_factor=0.3611, shift_factor=0.1159)
        return _instantiate(AutoencoderKL, convert_ldm_vae_checkpoint(sd, cfg), torch.bfloat16, **cfg)
    raise ValueError(f"No loader for VAE family '{family}' yet")


def load_qwen3_text_encoder(path):
    """Qwen3 language model (Z-Image). Config is derived from the weight shapes."""
    from transformers import AutoTokenizer, Qwen3Config, Qwen3Model
    sd = load_file(path)
    sd = {k[len("model."):]: v for k, v in sd.items() if k.startswith("model.")}
    hidden = sd["embed_tokens.weight"].shape[1]
    head_dim = sd["layers.0.self_attn.q_norm.weight"].shape[0]
    cfg = Qwen3Config(
        vocab_size=sd["embed_tokens.weight"].shape[0],
        hidden_size=hidden,
        intermediate_size=sd["layers.0.mlp.gate_proj.weight"].shape[0],
        num_hidden_layers=len({k.split(".")[1] for k in sd if k.startswith("layers.")}),
        num_attention_heads=sd["layers.0.self_attn.q_proj.weight"].shape[0] // head_dim,
        num_key_value_heads=sd["layers.0.self_attn.k_proj.weight"].shape[0] // head_dim,
        head_dim=head_dim,
        max_position_embeddings=40960,
        rms_norm_eps=1e-6,
        rope_theta=1000000.0,
        tie_word_embeddings=True,
    )
    with init_empty_weights():
        model = Qwen3Model(cfg)
    model.load_state_dict(sd, assign=True, strict=False)
    model.to(torch.bfloat16).eval()
    tokenizer = AutoTokenizer.from_pretrained(os.path.join(ASSETS, "qwen_tokenizer"))
    tokenizer.chat_template = QWEN3_CHAT_TEMPLATE
    return model, tokenizer


def resolve_hf_folder(path):
    """A Hugging Face model folder (config.json + shards). Accepts the folder, a shard or the index file."""
    folder = path if os.path.isdir(path) else os.path.dirname(path)
    if not os.path.isfile(os.path.join(folder, "config.json")):
        raise ValueError(f"{folder} is not a Hugging Face model folder (no config.json)")
    return folder


def load_qwen25vl_text_encoder(path):
    """Qwen2.5-VL (Qwen-Image / Qwen-Image-Edit) from a Hugging Face folder, plus its tokenizer/processor."""
    from transformers import AutoTokenizer, Qwen2_5_VLForConditionalGeneration, Qwen2VLProcessor
    folder = resolve_hf_folder(path)
    model = Qwen2_5_VLForConditionalGeneration.from_pretrained(folder, torch_dtype=torch.bfloat16).eval()
    root = os.path.dirname(folder)  # the repo layout keeps tokenizer/ and processor/ next to text_encoder/
    tok_dir = next((d for d in (os.path.join(root, "tokenizer"), folder) if glob.glob(os.path.join(d, "vocab.json"))),
                   os.path.join(ASSETS, "qwen_tokenizer"))
    proc_dir = os.path.join(root, "processor")
    tokenizer = AutoTokenizer.from_pretrained(tok_dir)
    processor = Qwen2VLProcessor.from_pretrained(proc_dir) if os.path.isdir(proc_dir) else None
    return model, tokenizer, processor


def qwen_image_scheduler():
    from diffusers import FlowMatchEulerDiscreteScheduler
    return FlowMatchEulerDiscreteScheduler(
        num_train_timesteps=1000, shift=1.0, use_dynamic_shifting=True,
        base_shift=0.5, max_shift=0.9, base_image_seq_len=256, max_image_seq_len=8192,
        shift_terminal=0.02, time_shift_type="exponential",
    )


def z_image_scheduler():
    from diffusers import FlowMatchEulerDiscreteScheduler
    return FlowMatchEulerDiscreteScheduler(num_train_timesteps=1000, shift=3.0, use_dynamic_shifting=False)
