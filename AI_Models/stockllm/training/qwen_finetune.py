"""Foundation script: Qwen specialization via QLoRA -- NOT part of V1 runs.

This is the scaffold for the future fine-tuning step.  It takes the synthetic
reasoning dataset (training/build_reasoning_dataset.py) and SFTs a local Qwen
with 4-bit QLoRA, sized for the RTX 5070 (12 GB VRAM) / 32 GB RAM setup.

Extras needed (install once, they are NOT in requirements.txt for V1):
    pip install transformers peft bitsandbytes accelerate trl datasets

Run (after building the dataset):
    python main.py finetune --dataset datasets/reasoning_train.jsonl

`--dry-run` only builds and prints a sample batch so you can validate the
dataset and formats without spending compute.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from config import DATASETS_DIR, MODELS_DIR
from utils.logging import get_logger

log = get_logger(__name__)

DEFAULT_BASE_MODEL = "Qwen/Qwen2.5-7B-Instruct"


def load_examples(dataset: str) -> list[dict]:
    path = Path(dataset)
    if not path.exists():
        raise FileNotFoundError(f"dataset not found: {path}")
    examples = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                examples.append(json.loads(line))
    return examples


def _build_training_args(parser: argparse.Namespace) -> dict:
    return dict(
        output_dir=parser.output_dir,
        per_device_train_batch_size=2,
        gradient_accumulation_steps=4,
        learning_rate=2e-4,
        warmup_steps=20,
        num_train_epochs=parser.epochs,
        logging_steps=10,
        save_steps=200,
        bf16=True,
        report_to=[],
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="QLoRA SFT for Qwen (foundation)")
    ap.add_argument("--dataset", default=str(DATASETS_DIR / "reasoning_train.jsonl"))
    ap.add_argument("--base-model", default=DEFAULT_BASE_MODEL)
    ap.add_argument("--output-dir", default=str(MODELS_DIR / "qwen-lora"))
    ap.add_argument("--lora-r", type=int, default=16)
    ap.add_argument("--lora-alpha", type=int, default=32)
    ap.add_argument("--epochs", type=int, default=2)
    ap.add_argument("--dry-run", action="store_true",
                    help="only validate the dataset and print a sample")
    args = ap.parse_args(argv)

    examples = load_examples(args.dataset)
    log.info("loaded %d examples from %s", len(examples), args.dataset)
    sample = examples[0]
    log.info("sample instruction: %s", sample["instruction"][:200])
    log.info("sample response: %s", sample["response"][:200])

    if args.dry_run:
        log.info("dry-run OK -- dataset is valid; nothing trained")
        return 0

    try:
        import torch  # noqa: F401
        from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig
        from trl import SFTConfig, SFTTrainer
        from peft import LoraConfig
    except ImportError as exc:
        log.error(
            "missing fine-tuning libraries (%s). Install extras first:\n"
            "    pip install transformers peft bitsandbytes accelerate trl datasets\n"
            "then rerun.", exc)
        return 2

    from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig
    from trl import SFTConfig, SFTTrainer
    from peft import LoraConfig

    bnb = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_compute_dtype=torch.bfloat16,
        bnb_4bit_use_double_quant=True,
    )
    tokenizer = AutoTokenizer.from_pretrained(args.base_model, trust_remote_code=True)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token
    model = AutoModelForCausalLM.from_pretrained(
        args.base_model, quantization_config=bnb, device_map="auto",
        trust_remote_code=True, torch_dtype=torch.bfloat16,
    )
    lora = LoraConfig(
        r=args.lora_r,
        lora_alpha=args.lora_alpha,
        lora_dropout=0.05,
        bias="none",
        task_type="CAUSAL_LM",
    )

    def format_chat(ex):
        return {
            "text": tokenizer.apply_chat_template(
                [{"role": "user", "content": ex["instruction"]},
                 {"role": "assistant", "content": ex["response"]}],
                tokenize=False, add_generation_prompt=False,
            )
        }

    from datasets import Dataset
    train_ds = Dataset.from_list([format_chat(e) for e in examples])

    sft_args = SFTConfig(
        output_dir=args.output_dir,
        per_device_train_batch_size=2,
        gradient_accumulation_steps=4,
        learning_rate=2e-4,
        warmup_steps=20,
        num_train_epochs=args.epochs,
        logging_steps=10,
        save_steps=200,
        bf16=True,
        report_to=[],
        max_seq_length=2048,
        dataset_text_field="text",
    )
    trainer = SFTTrainer(
        model=model,
        args=sft_args,
        train_dataset=train_ds,
        peft_config=lora,
        processing_class=tokenizer,
    )
    trainer.train()
    trainer.save_model(args.output_dir)
    log.info("adapter saved to %s -- merge with base model for serving", args.output_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
