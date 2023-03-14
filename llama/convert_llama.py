# Copyright 2022 EleutherAI and The HuggingFace Inc. team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import argparse
import json
import os
import shutil

import torch
import hiq


"""
Sample usage:

    ```
    python src/transformers/models/llama/convert_llama_weights_to_hf.py \
        --input_dir /path/to/downloaded/llama/weights --model_size 7B --output_dir /output/path
    ```

Thereafter, models can be loaded via:

    ```
    tokenizer = transformers.LLaMATokenizer.from_pretrained("/output/path/tokenizer/")

    model = transformers.LLaMAForCausalLM.from_pretrained("/output/path/llama-7b/")
    ```
"""

INTERMEDIATE_SIZE_MAP = {
    "7B": 11008,
    "13B": 13824,
    "30B": 17920,
    "65B": 22016,
}
NUM_SHARDS = {
    "7B": 1,
    "13B": 2,
    "30B": 4,
    "65B": 8,
}


def read_json(path):
    with open(path, "r") as f:
        return json.load(f)


def write_json(text, path):
    with open(path, "w") as f:
        json.dump(text, f)


def write_model(model_path, input_base_path, model_size):
    assert model_size in INTERMEDIATE_SIZE_MAP
    os.makedirs(model_path, exist_ok=True)

    params = read_json(os.path.join(input_base_path, "params.json"))
    num_shards = NUM_SHARDS[model_size]
    n_layers = params["n_layers"]
    n_heads = params["n_heads"]
    n_heads_per_shard = n_heads // num_shards
    dim = params["dim"]
    dims_per_head = dim // n_heads
    base = 10000.0
    inv_freq = 1.0 / (
        base ** (torch.arange(0, dims_per_head, 2).float() / dims_per_head)
    )

    # permute for sliced rotary
    def permute(w):
        return (
            w.view(n_heads, dim // n_heads // 2, 2, dim)
            .transpose(1, 2)
            .reshape(dim, dim)
        )

    # Load weights
    if model_size == "7B":
        # Not shared
        # (The sharded implementation would also work, but this is simpler.)
        loaded = torch.load(
            os.path.join(input_base_path, "consolidated.00.pth"), map_location="cpu"
        )
    else:
        # Sharded
        loaded = [
            torch.load(
                os.path.join(input_base_path, f"consolidated.{i:02d}.pth"),
                map_location="cpu",
            )
            for i in range(num_shards)
        ]
    param_count = 0
    index_dict = {"weight_map": {}}
    for layer_i in range(n_layers):
        filename = "pytorch_model-{:05d}-of-{:05d}.bin".format(
            layer_i + 1,
            n_layers + 1,
        )
        if model_size == "7B":
            # Unsharded
            state_dict = {
                f"model.layers.{layer_i}.self_attn.q_proj.weight": permute(
                    loaded[f"layers.{layer_i}.attention.wq.weight"]
                ),
                f"model.layers.{layer_i}.self_attn.k_proj.weight": permute(
                    loaded[f"layers.{layer_i}.attention.wk.weight"]
                ),
                f"model.layers.{layer_i}.self_attn.v_proj.weight": loaded[
                    f"layers.{layer_i}.attention.wv.weight"
                ],
                f"model.layers.{layer_i}.self_attn.o_proj.weight": loaded[
                    f"layers.{layer_i}.attention.wo.weight"
                ],
                f"model.layers.{layer_i}.mlp.gate_proj.weight": loaded[
                    f"layers.{layer_i}.feed_forward.w1.weight"
                ],
                f"model.layers.{layer_i}.mlp.down_proj.weight": loaded[
                    f"layers.{layer_i}.feed_forward.w2.weight"
                ],
                f"model.layers.{layer_i}.mlp.up_proj.weight": loaded[
                    f"layers.{layer_i}.feed_forward.w3.weight"
                ],
                f"model.layers.{layer_i}.input_layernorm.weight": loaded[
                    f"layers.{layer_i}.attention_norm.weight"
                ],
                f"model.layers.{layer_i}.post_attention_layernorm.weight": loaded[
                    f"layers.{layer_i}.ffn_norm.weight"
                ],
            }
        else:
            # Sharded
            state_dict = {
                f"model.layers.{layer_i}.input_layernorm.weight": loaded[0][
                    f"layers.{layer_i}.attention_norm.weight"
                ],
                f"model.layers.{layer_i}.post_attention_layernorm.weight": loaded[0][
                    f"layers.{layer_i}.ffn_norm.weight"
                ],
            }
            state_dict[f"model.layers.{layer_i}.self_attn.q_proj.weight"] = permute(
                torch.cat(
                    [
                        loaded[i][f"layers.{layer_i}.attention.wq.weight"].view(
                            n_heads_per_shard, dims_per_head, dim
                        )
                        for i in range(num_shards)
                    ],
                    dim=0,
                ).reshape(dim, dim)
            )
            state_dict[f"model.layers.{layer_i}.self_attn.k_proj.weight"] = permute(
                torch.cat(
                    [
                        loaded[i][f"layers.{layer_i}.attention.wk.weight"].view(
                            n_heads_per_shard, dims_per_head, dim
                        )
                        for i in range(num_shards)
                    ],
                    dim=0,
                ).reshape(dim, dim)
            )
            state_dict[f"model.layers.{layer_i}.self_attn.v_proj.weight"] = torch.cat(
                [
                    loaded[i][f"layers.{layer_i}.attention.wv.weight"].view(
                        n_heads_per_shard, dims_per_head, dim
                    )
                    for i in range(num_shards)
                ],
                dim=0,
            ).reshape(dim, dim)

            state_dict[f"model.layers.{layer_i}.self_attn.o_proj.weight"] = torch.cat(
                [
                    loaded[i][f"layers.{layer_i}.attention.wo.weight"]
                    for i in range(num_shards)
                ],
                dim=1,
            )
            state_dict[f"model.layers.{layer_i}.mlp.gate_proj.weight"] = torch.cat(
                [
                    loaded[i][f"layers.{layer_i}.feed_forward.w1.weight"]
                    for i in range(num_shards)
                ],
                dim=0,
            )
            state_dict[f"model.layers.{layer_i}.mlp.down_proj.weight"] = torch.cat(
                [
                    loaded[i][f"layers.{layer_i}.feed_forward.w2.weight"]
                    for i in range(num_shards)
                ],
                dim=1,
            )
            state_dict[f"model.layers.{layer_i}.mlp.up_proj.weight"] = torch.cat(
                [
                    loaded[i][f"layers.{layer_i}.feed_forward.w3.weight"]
                    for i in range(num_shards)
                ],
                dim=0,
            )

        state_dict[f"model.layers.{layer_i}.self_attn.rotary_emb.inv_freq"] = inv_freq
        for k, v in state_dict.items():
            index_dict["weight_map"][k] = filename
            param_count += v.numel()
        torch.save(state_dict, os.path.join(model_path, filename))

    filename = "pytorch_model-{:05d}-of-{:05d}.bin".format(
        n_layers + 1,
        n_layers + 1,
    )
    if model_size == "7B":
        # Unsharded
        state_dict = {
            "model.embed_tokens.weight": loaded["tok_embeddings.weight"],
            "model.norm.weight": loaded["norm.weight"],
            "lm_head.weight": loaded["output.weight"],
        }
    else:
        state_dict = {
            "model.norm.weight": loaded[0]["norm.weight"],
            "model.embed_tokens.weight": torch.cat(
                [loaded[i]["tok_embeddings.weight"] for i in range(num_shards)], dim=1
            ),
            "lm_head.weight": torch.cat(
                [loaded[i]["output.weight"] for i in range(num_shards)], dim=0
            ),
        }

    for k, v in state_dict.items():
        index_dict["weight_map"][k] = filename
        param_count += v.numel()
    torch.save(state_dict, os.path.join(model_path, filename))

    # Write configs
    index_dict["metadata"] = {"total_size": param_count * 2}
    write_json(index_dict, os.path.join(model_path, "pytorch_model.bin.index.json"))
    config_out = {
        "architectures": ["LLaMAForCausalLM"],
        "bos_token_id": 0,
        "eos_token_id": 1,
        "hidden_act": "silu",
        "hidden_size": params["dim"],
        "intermediate_size": INTERMEDIATE_SIZE_MAP[model_size],
        "initializer_range": 0.02,
        "max_sequence_length": 2048,
        "model_type": "llama",
        "num_attention_heads": params["n_heads"],
        "num_hidden_layers": params["n_layers"],
        "pad_token_id": -1,
        "rms_norm_eps": params["norm_eps"],
        "torch_dtype": "float16",
        "transformers_version": "4.27.0.dev0",
        "use_cache": True,
        "vocab_size": 32000,
    }
    write_json(
        config_out,
        os.path.join(model_path, "config.json"),
    )
    generation_config = {
        "_from_model_config": True,
        "bos_token_id": 0,
        "eos_token_id": 1,
        "pad_token_id": 0,
        "transformers_version": "4.27.0.dev0",
    }
    write_json(
        generation_config,
        os.path.join(model_path, "generation_config.json"),
    )


def write_tokenizer(tokenizer_path, input_tokenizer_path):
    os.makedirs(tokenizer_path, exist_ok=True)
    write_json({}, os.path.join(tokenizer_path, "special_tokens_map.json"))
    write_json(
        {
            "bos_token": "",
            "eos_token": "",
            "model_max_length": int(1e30),
            "tokenizer_class": "LLaMATokenizer",
            "unk_token": "",
        },
        os.path.join(tokenizer_path, "tokenizer_config.json"),
    )
    shutil.copyfile(
        input_tokenizer_path, os.path.join(tokenizer_path, "tokenizer.model")
    )

def convert_llama_meta():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tokenizer_path", type=str, required=True)
    parser.add_argument("--model", type=str, required=True, choices=NUM_SHARDS.keys())
    parser.add_argument("--output_path", type=str, required=True)
    args = parser.parse_args()

    assert "tokenizer.model" in os.listdir(args.tokenizer_path), "Tokenizer model not found in llama path"
    for model in models:
        if model not in os.listdir(args.tokenizer_path):
            print(f"[WARN] Model {model} not found in llama path")
    assert args.model in os.listdir(args.tokenizer_path), f"Model {args.model} not found in llama path"
    
    output_path = os.path.join(args.output_path, args.model)
    os.makedirs(output_path, exist_ok=True)

    if "tokenizer.model" not in os.listdir(output_path):
        shutil.copy(os.path.join(args.tokenizer_path, "tokenizer.model"), args.output_path)

    model_path, tokenizer_path, output_path = os.path.join(args.tokenizer_path, args.model),os.path.join(args.output_path, "tokenizer.model"), output_path
    
    checkpoints = sorted(Path(model_path).glob("*.pth"))
    with open(Path(model_path) / "params.json", "r") as f:
        params = json.loads(f.read())
    
    model_args = ModelArgs(
        max_seq_len=2048,
        max_batch_size=1,
        **params
    )

    tokenizer = Tokenizer(model_path=tokenizer_path)
    model_args.vocab_size = tokenizer.n_words

    with init_empty_weights():
        torch.set_default_tensor_type(torch.HalfTensor)
        model = Transformer(model_args)
    torch.set_default_tensor_type(torch.FloatTensor)

    key_to_dim = {"w1": 0, "w2": -1,"w3": 0,"wo": -1,"wq": 0,"wk": 0,"wv": 0,"output": 0,"tok_embeddings": -1,
        "ffn_norm": None,        "attention_norm": None,        "norm": None,        "rope": None
    }

    dt = {}

    for i, ckpt in tqdm(enumerate(checkpoints), total=len(checkpoints)):
        checkpoint = torch.load(ckpt, map_location="cpu")
        for parameter_name, parameter in model.named_parameters():
            if parameter_name not in dt:
                dt[parameter_name] = torch.zeros_like(parameter, device="cpu")
            short_name = parameter_name.split(".")[-2]
            if key_to_dim[short_name] is None and i == 0:
                dt[parameter_name] = checkpoint[parameter_name]
            elif key_to_dim[short_name] == 0:
                size = checkpoint[parameter_name].size(0)
                dt[parameter_name][size * i : size * (i + 1), :] = checkpoint[
                    parameter_name
                ]
            elif key_to_dim[short_name] == -1:
                size = checkpoint[parameter_name].size(-1)
                dt[parameter_name][:, size * i : size * (i + 1)] = checkpoint[
                    parameter_name
                ]
            del checkpoint[parameter_name]
        del checkpoint
    with open(os.path.join(output_path, "params.json"), "w") as f:
        f.write(json.dumps(params, indent=4))

    torch.save(dt, os.path.join(output_path, "state_dict.pth"))


def convert_llama_hf():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--input_dir",
        help="Location of LLaMA weights, which contains tokenizer.model and model folders",
    )
    parser.add_argument(
        "--model_size",
        choices=["7B", "13B", "30B", "65B"],
    )
    parser.add_argument(
        "--output_dir",
        help="Location to write HF model and tokenizer",
    )
    args = parser.parse_args()
    write_model(
        model_path=os.path.join(
            args.output_dir, "llama-{}".format(args.model_size).lower()
        ),
        input_base_path=os.path.join(args.input_dir, args.model_size),
        model_size=args.model_size,
    )
    write_tokenizer(
        tokenizer_path=os.path.join(args.output_dir, "tokenizer"),
        input_tokenizer_path=os.path.join(args.input_dir, "tokenizer.model"),
    )


if __name__ == "__main__":
    convert_llama_meta()