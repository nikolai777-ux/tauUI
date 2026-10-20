"""Which input of a model each connected picture goes to - one rule for every engine.

Pictures arrive in wire order (a Merge's in index order), each with the name of the node it comes from.
  - By default they all go to the model's main picture input: diffusers' `image` (one picture as itself, several as
    a list - what a list means is the pipeline's own business: tested, Qwen-Image-2.1 and FireRed / Qwen-Image-
    Edit-Plus take a list as reference pictures), stable-diffusion.cpp's repeatable -r / --ref-image.
  - A picture whose node is NAMED after another picture input the library declares goes there instead: a diffusers
    __call__ parameter (last_image, mask_image, control_image, ...) or an sd-cli flag from its own --help
    (end_img -> --end-img, init_img -> -i). Digits the app adds to keep names unique are ignored (last_image2).
    Nothing here lists models: the names are the library's own, and naming the node is the user's explicit choice.
The routing is always reported, and so is any picture the model has no input for.
"""

import re


def base_name(name):
    """A node's name without the digits added to keep names unique, lower-case, '-' as '_': 'End_Img2' -> 'end_img'."""
    return re.sub(r"\d+$", "", (name or "").strip()).lower().replace("-", "_")


def route(named, declared, default):
    """named: [(node name, path)] in order; declared: {base name: input} the model's own picture inputs; default: the
    input the others go to (None: the model takes no pictures). Returns ({input: [paths]}, report, unused names)."""
    routed, report, unused = {}, {}, []
    for name, path in named:
        target = declared.get(base_name(name)) or default
        if target is None:
            unused.append(name)
            continue
        routed.setdefault(target, []).append(path)
        report.setdefault(target, []).append(name)
    text = " · ".join(f"{target} ← {', '.join(names)}" for target, names in report.items())
    return routed, ("Pictures: " + text if text else ""), unused
