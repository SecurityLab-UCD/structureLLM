# PPO process that receive seeds from fuzzer then send back the mutated seeds to fuzzer
from dataclasses import dataclass, field
from typing import Optional
import os
import torch
import tyro
from accelerate import Accelerator
from peft import LoraConfig
from tqdm import tqdm
from transformers import AutoTokenizer, BitsAndBytesConfig

from trl import (
    AutoModelForCausalLMWithValueHead,
    PPOConfig,
    set_seed,
)
import threading
import re
import sysv_ipc
import struct
import random

tqdm.pandas()

TYPE_SEED = 1
TYPE_EMPTY_SEED = 2
TYPE_REWARD = 3
TYPE_REQUEST = 4

access_token = "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"
cur_path = os.path.dirname(os.path.realpath(__file__))
output_dir = os.path.join(cur_path, "ppo_checkpoint")
message_queue = []
seed_id_map = {}
id_rwd_map = {}
seeds_from_fuzzer = set()
uid = 1
shared_resource_lock = threading.Lock()

@dataclass
class ScriptArguments:
    """
    Setup experiment config
    """
    fuzzing_target: Optional[str] = field(default='libpng')
    if_mixed_model: Optional[bool] = field(default=True)
    peft_config: Optional[LoraConfig] = field(
        default_factory=lambda: LoraConfig(
            r=64,
            lora_alpha=16,
            lora_dropout=0.1,
            bias="none",
            target_modules=[
                "q_proj",
                "down_proj",
                "gate_proj",
                "o_proj",
                "k_proj",
                "v_proj",
                "up_proj",
            ],
            task_type="CAUSAL_LM",
        ),
    )
    trust_remote_code: bool = field(
        default=True, metadata={"help": "Enable `trust_remote_code`"}
    )


args = tyro.cli(ScriptArguments)

def mq_thread():
    """
    Thread to receive request from fuzzer, and send generated seed to fuzzer
    """
    global message_queue, seed_id_map, seeds_from_fuzzer
    try:
        mq = sysv_ipc.MessageQueue(1234, sysv_ipc.IPC_CREAT)
    except sysv_ipc.ExistentialError:
        print(f"Message queue with key {1234} already exists.")
        return
    while True:
        # only receive request msg
        try:
            msg, mtype = mq.receive(type=TYPE_REQUEST)
            if msg != b'':
                if len(seeds_from_fuzzer)>30:
                    seeds_from_fuzzer.clear()
                seeds_from_fuzzer.add(msg.decode(errors='ignore')[4:])
            while message_queue !=[]:
                # send uid + seed
                seed = message_queue.pop(0)
                mq.send(
                    struct.pack("I", seed_id_map[seed]) + seed.encode("utf-8"),
                    True,
                    type=TYPE_SEED,
                )
        except RuntimeError as e:
            print(e)

def hex_string_to_hex(hex_string,fuzzing_target):
    """
    Formatting generated hex string.

    Returns:
        String of hex.
    """
    if len(hex_string.split("### Output:"))>=2:
        hex_string =hex_string.split("### Output:")[1]
    else:
        hex_string = hex_string.replace(f"### Input: ```Based on below hex {fuzzing_target} seed, mutate a new {fuzzing_target} seed. Make sure the example is complete and valid.", " ")

    hex_string = re.sub(r"[^a-zA-Z0-9\s]", " ", hex_string)
    hex_values = hex_string.replace("0x", " ")
    # Split the string into sections
    sections = hex_values.split()
    # Iterate through the sections and add leading zeros if needed
    result = []
    for section in sections:
        if len(section) == 1:
            section = "0" + section
            result.append(section)
        elif len(section) == 2:
            result.append(section)
    result = "".join(result)
    if len(result)>2040: #limite seed size to 2048
        result = result[:2040]
    return result


def main():
    """
    Main function to run PPO loop
    """
    model_name = f"llama-2-7b-structured-{args.fuzzing_target}-hex-mutator"
    if args.if_mixed_model:
        model_name = f"llama-2-7b-structured-{args.fuzzing_target}-mix-hex-mutator"
    # Init the tokenizer and dataset
    tokenizer = AutoTokenizer.from_pretrained(
        os.path.join(cur_path, model_name),
        use_fast=True,
        token=access_token,
    )
    # Some tokenizers like GPT-2's don't have a padding token by default, so we set one here.
    # tokenizer.pad_token_id = tokenizer.eos_token_id
    tokenizer.pad_token = tokenizer.bos_token
    # tokenizer.padding_side = "left"
    # We retrieve the dataloader by calling the `build_dataset` function.

    # set seed before initializing value head for deterministic eval
    set_seed(0)

    # Build the model.
    peft_config = args.peft_config
    # Copy the model to each device
    current_device = Accelerator().local_process_index
    device_map = {"": current_device}

    model = AutoModelForCausalLMWithValueHead.from_pretrained(
        os.path.join(cur_path, model_name),
        trust_remote_code=args.trust_remote_code,
        device_map=device_map,
        peft_config=peft_config,
        token=access_token,
        torch_dtype=torch.bfloat16,
        quantization_config=BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_quant_type="nf4",
            bnb_4bit_compute_dtype=torch.bfloat16,
            bnb_4bit_use_double_quant=True,
        ),
        # use_flash_attention_2=True, Unable to use this feature in current GPU
    )
    # Whether or not the model should use the past last key/values attentions (if applicable to the model) to speed up decoding.
    model.config.use_cache = False
    model.config.pretraining_tp = 1

    
    # flash attention 1
    torch.backends.cuda.sdp_kernel(
        enable_flash=True, enable_math=False, enable_mem_efficient=False
    )
    example={"bloaty":"0xcf0xfa,0xed0xfe,0xbb0x1,0x10x0,0x00xf8,0xff0xdf,0xb0x0,0x80x0,0x10x0,0x00x0,0x10x0,0xf70x82,0x100x3a,0x30x3,0x00x2,0xfd0xb,0x190x0,0x00x0,0x570x0,0x00x0,0x400x0,0x00x0,0xff0xff,0x00x7f,0x450x4c,0x460x1,0xbb0xe8,0xff0xff,0xff0x6,0xff0xff,0xff0x3,0x00x0,0xff0xff,0xff0xff,0xff0x0,0x800xff,0x00x0,0x00x0,0x00x0,0x00x0,0x00x0,0x00x0,0x00x0,0x00x0,0x10x0,0xdf0x34,0x00x0,0x800x0,0x00x0,0x00x0,0x00x35,0x00x0,0x00x0,0x00x0,0x350x0,0x00x0,0x350x0,0x00x0,0x00x0,0x0", "libpng":"0x890x50,0x4e0x47,0xd0xa,0x1a0xa,0x00x0,0x00xd,0x490x48,0x440x52,0x00x0,0x00x1,0x00x0,0x00x1,0x20x3,0x00x0,0x10x3e,0xb30xd8,0x210x0,0x00x0,0x60x50,0x4c0x54,0x450xee,0xff0x22,0x220x66,0xff0x6c,0x20xd2,0x260x0,0x00x0,0x290x49,0x440x41,0x540x8,0xd70x63,0x600x84,0x820xf,0x500x0,0xd0x49,0x480x44,0x580x4e,0x900xe0,0x20xd5,0x70x89,0x500x4e,0x470xd,0xa0x1a,0xa0x0,0x00x0,0xd0x49,0x480x44,0x520x0,0x00x0,0x200x0,0x00x0,0x20x1,0x30x0,0x00x1,0x3e0xb3,0xd80x21,0x00x0,0x00x6","libjpg":"0xff,0xd8,0xff,0xee,0x0,0xe,0x41,0x64,0x6f,0x5b,0x62,0x1,0x66,0x1,0x1,0x1,0x1,0x4,0xff,0xc3,0x0,0x11,0x8,0x0,0xe0,0x0,0xa2,0x3,0x52,0x11,0x1,0x47,0x12,0x0,0x42,0x32,0x0,0xff,0xc4,0x0,0x15,0x0,0x1,0x1,0x0,0x0,0x0,0x0,0x0,0x0,0x0,0x0,0x0,0x0,0x0,0x0,0x0,0x0,0x0,0x1,0xff,0xda,0x0,0xc,0x3,0x52,0x1,0x47,0x1,0x42,0x3,0x5,0x0,0x0,0x0,0x0,0x0,0x0,0x0,0x0,0x1,0xea,0x3,0x11,0x0,0x4,0x12,0x21,0x31,0x22,0x41,0x51,0x71,0x13,0x14,0x33,0x61,0xa1,0xff,0xff,0xda,0x4b,0x44,0x4d,0x57,0x0,0x10,0x41,0x3f,0x58,0x53","libjpeg":"0xff,0xd8,0xff,0xee,0x0,0xe,0x41,0x64,0x6f,0x5b,0x62,0x1,0x66,0x1,0x1,0x1,0x1,0x4,0xff,0xc3,0x0,0x11,0x8,0x0,0xe0,0x0,0xa2,0x3,0x52,0x11,0x1,0x47,0x12,0x0,0x42,0x32,0x0,0xff,0xc4,0x0,0x15,0x0,0x1,0x1,0x0,0x0,0x0,0x0,0x0,0x0,0x0,0x0,0x0,0x0,0x0,0x0,0x0,0x0,0x0,0x1,0xff,0xda,0x0,0xc,0x3,0x52,0x1,0x47,0x1,0x42,0x3,0x5,0x0,0x0,0x0,0x0,0x0,0x0,0x0,0x0,0x1,0xea,0x3,0x11,0x0,0x4,0x12,0x21,0x31,0x22,0x41,0x51,0x71,0x13,0x14,0x33,0x61,0xa1,0xff,0xff,0xda,0x4b,0x44,0x4d,0x57,0x0,0x10,0x41,0x3f,0x58,0x53","zlib":"0x680x81,0x3c0x98,0x300x68,0x10xe0,0xff0xfd,0x760xff,0x30x0,0xfb0x68,0x00xf8,0xff0xff,0xff0x0,0x680x0,0x00x0,0x00x0,0x810x65,0x00x68,0x650x6c,0x6c0x6f,0x2c0x20,0x680x65,0x6c0x7f,0x6f0x21,0x00x0,0xff0xff,0xff0xff,0xff0xff,0xff0xff,0xff0x6b,0x810xb7,0x680x81,0x650x0,0x900x0,0xfa0xff,0xde0x0,0x00x68,0x00xf8,0xff0xff,0xff0x0,0x680x0,0x00x81,0x680x69,0x810x81,0x680x69,0x00x0,0x00x81,0x650x0,0x680x65,0x6c0x6c,0x6f0x2c,0x200x68,0x650x6c,0x7f0x6f,0x210x0,0x00x0,0x00x0,0x00x0,0x00xed,0x00x0,0x00x0,0x00x0,0x00x0,0x00x0,0x00x0,0x00x0,0x00x0,0x00x0,0x00x0,0x00x0,0x00x0,0x00x0,0x00x0,0x0","libtiff":"0x490x49,0x2a0x0,0x620x0,0x00x0,0x740xa5,0xc20xae,0xce0xa,0xbf0x9,0x130x42,0x580x2d,0x2b0xbf,0xee0x20,0xb70x1b,0x430x5f,0xff0x23,0x840xb0,0xb0xed,0x950xb1,0x5f0xf5,0x9b0x70,0x530x5b,0x200x62,0x90xca,0x530x7a,0x260x39,0x7e0xe4,0xfa0xf,0xf0xf,0xf0xf,0xa80x6e,0x6e0x2f,0xc40x49,0x300xc8,0x3d0x55,0x7e0x4e,0x880x6e,0x500x1,0x9f0x64,0x690x6b,0x5d0xa8,0xd20xe9,0x480x88,0xbf0xfb,0x2f0xf,0xae0x0,0x00x0,0x00x30,0x6b0x1b,0x810x1,0x120x0,0x00x1,0x30x0,0x10x0,0x00x0,0x30x0,0x10x1,0x10x1,0x30x0,0x10x0,0x00x0,0x1d0x0,0x00x0,0x20x1,0x30x0,0x10x0,0x00x0,0x100x0,0x00x0,0x30x1,0x30x0,0x10x0,0x00x0,0x80x0,0x00x0,0x150x1,0x30x0,0x10x0,0x00x0,0x60x1,0x10x0,0x3d0x1,0x30x0,0x10x0,0x00x0,0x20x0,0x00x0,0xd0x1,0x20x0,0xf0x0,0x00x0,0x400x1,0x00x0,0x110x1,0x40x0,0x10x0,0x00x0,0x170x1,0x00x0,0xd0x0,0x1c0x0,0x10x0,0x00x0,0x10x0,0x00x1,0xa0x0,0x30x0,0x10x0,0x00x0,0x20x1,0x10x0,0x160x1,0x30x0,0x10x0,0x00x0,0x10x0,0xec0x0,0xa0x0,0x1c0x1,0x00x0,0x00x0,0x00x1,0x00x1,0x160x1,0x1c0x1,0x00x1,0x10x1,0x90x1,0x00x1,0x170x1,0x40x0,0x00x1,0x10x0,0x800x0,0x00x0,0x1a0x1,0x80x0,0x10x0,0x00x0,0x480x1,0x00x1,0x1b0x1,0x50x0,0x10x0,0x00x0,0x5a0x0,0x00x0,0x1c0x1,0x30x0,0x10x0,0x00x0,0x10x0,0x00x0,0x280x1,0x30x0,0x10x0,0x00x0,0x10x0,0x10x0,0x240x0,0x1c0x1,0x20x0,0x10x1,0x10x0,0x10x0,0x00x0,0x10x1,0x620xc2,0xc20xc2",'freetype':"0x10x0,0x40x4,0x00x1,0x10x1,0x1c0x4f,0x700x61,0x720x4b,0x6d0x6e,0x620x55,0x610x79,0x750x4b,0x450x59,0x540x4a,0x6d0x65,0x2c0x47,0x650x75,0x6c0x6c,0x610x72,0x00x1,0x10x1,0x1f0xf8,0xf0x0,0xf80x1b,0x10xf8,0x1c0x2,0xf80x18,0x40xbd,0xfb0x5c,0xf80xba,0xf90xb4,0x50xf7,0x220xf0,0x940xf7,0xf10x12,0xf70x2b,0x110x0,0x20x1,0x10x21,0x3e0x43,0x6f0x70,0x790x6b,0x750x79,0x680x69,0x90x28,0x430x29,0x3a0x39,0x310x30,0x350x25,0x580x6e,0x690x63,0x6f0x65,0x650x2d,0x90x51,0x7c0x64,0x2c0x4b,0x720x63,0x770x5a,0x6b0x69,0x630x5a,0x650x79,0x7a0x9,0x480x41,0x520x4e,0x200x4f,0x6e0x65,0x200x80,0xff0x67,0x750x73,0x620x74,0x00x0,0x10x1,0x00x0,0x350x0,0x910x0,0x790x1,0x660x0,0x700x1,0x4a0x1,0x780x1,0x630x1,0x730x0,0x5a0x0,0x780x0,0x6d0x1,0x610x1,0x4b0x1,0x660x1,0x710x0,0x10x1,0x10x1,0x10x1,0x1a0x84,0x400x88,0x1a0x18,0x880x6,0x680xa,0x280x5f,0x10x28,0x00x1,0x720x6f,0x630x6b,0x720x69,0x640x67,0x650x7c,0xf70x0,0x10x0,0x10x1,0x10x1,0x10x1,0x10x1,0x10x1,0x10x1,0x10x1,0x10x1,0x3e0x43,0x6f0x70,0x790xd1,0x150x45,0xcd0x69,0xfb0x3a,0xf30x7,0xf70x3a,0xf70x15,0x150x6a,0x460x66,0xcd0x6a,0xfb0x3a,0xbd0xb2,0xcf0x50,0x10x1,0xff0x0,0x820xfc,0xa0x78,0x680x13,0x130x49,0x530x4f,0x4c0x61,0x740x69,0x6e0x31,0x450x70,0x660x75,0x630x69,0x670x6e,0xc0x1d,0xc0xc,0xc0xc,0xc0xc,0xc0xc,0xc0xc,0xc0xc,0xc0xc,0x2c0xc,0xc0xc,0xc0xc,0xc0xef,0xfc0xec,0xef0xf8,0xec0xef,0x270xef,0xf70x5c,0xfc0x88,0x60xe,0xf70x5c,0xbd0x16,0xef0xf8,0x880x27,0x270x24,0x2a0x25,0x250x27,0x270x27,0x270x27,0x210x26,0x270x24,0x280x27,0x270x27,0x270x27,0x270x27,0x270x27,0x270x27,0x270x27,0x2a0x28,0x270x27,0x240x26,0x240x2a,0x290x24,0x280x24,0x210x29,0x210x21,0x250x24,0x260x21,0x240x24,0x280xff,0xff0xff,0xff0x25,0x280x23,0x260x29,0x230x23,0x290x27,0x250x21,0xc0x23,0x230x26,0x240x27,0x10x0,0xfe0xff,0x380x0,0x10x0,0x130x0,0x360x0,0x20x0,0x10xff,0x380x0,0x20x0,0x20x1,0xf40x0,0x30x0,0x10xff,0x38",'poppler':'0x250x50,0x440x46,0x2d0x31,0x2e0x34,0xa0x31,0x200x30,0x200x6f,0x620x6a,0x200xa,0x3c0x3c,0xa0x2f,0x500x61,0x670x65,0x730x20,0x320x20,0x300x20,0x520xa,0x320x20,0x300x20,0x6f0x62,0x6a0x20,0xa0x3c,0x3c0xa,0x2f0x52,0x650x73,0x6f0x75,0x720x63,0x650x73,0x200xa,0x3e0x3e,0xa0x2f,0x430x6f,0x6e0x74,0x650x6e,0x740x73,0x200x38,0x310x31,0x200x30,0x200x52,0xa0x38,0x310x31,0x200x30,0x200x6f,0x620x6a,0x200xa,0x3c0x3c,0xa0x2f,0x4c0x65,0x6e0x67,0x740x68,0x200x31,0x370x38,0x360x33,0xa0x3e,0x3e0xa,0x730x74,0x720x65,0x610x6d,0xa0x42,0x490xa,0x2f0x57,0x200x36,0x320xa,0x2f0x48,0x200x36,0x320xa,0x2f0x44,0x5b0x31,0xa0x30,0x5d0xa,0x2f0x46,0x2f0x43,0x430x46,0x8a0x2f,0x440x50,0x3c0x3c,0x2f0x4b,0x200x2d,0x310xa,0x740x72,0x610x69,0x6c0x65,0x720xa,0x3c0x3c,0xa0x2f,0x520x6f,0x6f0x74,0x200x31,0x200x30,0x200x52,0xa0x3e,0x3e0xa'}
    seed_queue=[example[args.fuzzing_target]]

    generation_kwargs = {
        "do_sample": True,
        "min_length": -1,
        "top_p": 0.92, # 0.9
        "top_k": 50,
        "temperature":1.25,
        "pad_token_id": tokenizer.bos_token_id,
    }

    while True:
        global seeds_from_fuzzer
        is_from_fuzzer = False
        current_seed = random.choice(seed_queue)
        if seeds_from_fuzzer:
            current_seed = seeds_from_fuzzer.pop()
            if len(seed_queue)>30:
                seed_queue = []
            seed_queue.append(current_seed)
            is_from_fuzzer = True

        formatted_chunks = []
        for i in range(0,len(current_seed),4):
            if i+3 < len(current_seed):
                formatted_chunks.append(f"0x{current_seed[i:i+2]}0x{current_seed[i+2:i+4]}")
            else:
                # If no pair, add the single element
                formatted_chunks.append(f"0x{current_seed[i:]}")
        prompt = "### Input: ```Based on below hex "+args.fuzzing_target+" seed, mutate a new "+args.fuzzing_target+" seed. Make sure the example is complete and valid. "+','.join(formatted_chunks)+"```"
        
        query_tensors = tokenizer(prompt, return_tensors="pt")["input_ids"].to('cuda')
        response_tensors = model.generate(
            input_ids=query_tensors,
            max_new_tokens=400,
            **generation_kwargs,
        )

        response = tokenizer.batch_decode(
            response_tensors, skip_special_tokens=True
        )
        # Compute sentiment score
        global uid, seed_id_map, id_rwd_map, message_queue
        for r in response:
            seed = hex_string_to_hex(r,args.fuzzing_target)
            seed_id_map[seed] = uid + os.getpid()
            # id_rwd_map[uid + os.getpid()] = float(0.0)
            message_queue.append(seed)
            if is_from_fuzzer:
                print("sff:::",seed)
            else:
                print("seed:::",seed)
        uid += 8
        torch.cuda.empty_cache()

if __name__ == "__main__":
    t = threading.Thread(
        target=mq_thread,
        args=(),
    )
    t.start()
    # if accelerator.is_main_process:
    # t2 = threading.Thread(target=reward_thread, args=())
    # t2.start()
    # time.sleep(7200)
    main()
