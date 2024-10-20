# Copyright (c) 2024 Microsoft
# Licensed under The MIT License [see LICENSE for details]

from transformers.modeling_flash_attention_utils import _flash_attention_forward
from transformers.models.llama.modeling_llama import *

from ..modules.quest import quest_decode_kernel
from ..ops.streaming_kernel import a_shape_kernel, tri_shape_kernel


def attn_forward(
    self,
    hidden_states: torch.Tensor,
    attention_mask: Optional[torch.LongTensor] = None,
    position_ids: Optional[torch.LongTensor] = None,
    output_attentions: bool = False,
    use_cache: bool = False,
    cache_position: Optional[torch.LongTensor] = None,
    position_embeddings: Optional[
        Tuple[torch.Tensor, torch.Tensor]
    ] = None,  # will become mandatory in v4.46
    past_key_value: Cache = None,
    prefill_forward=None,
    decoding_forward=None,
    customized_rope_func=None,
    attn_forward_config=None,
) -> Tuple[torch.Tensor, Optional[torch.Tensor], Optional[Tuple[torch.Tensor]]]:
    output_attentions = False

    bsz, q_len, _ = hidden_states.size()

    if "q_proj" in self.__dict__["_modules"]:
        query_states = self.q_proj(hidden_states)
        key_states = self.k_proj(hidden_states)
        value_states = self.v_proj(hidden_states)
    else:
        qkv = self.qkv_proj(hidden_states)
        query_pos = self.num_heads * self.head_dim
        key_value_pos = query_pos // self.num_key_value_groups
        query_states, key_states, value_states = torch.split(
            qkv, [query_pos, key_value_pos, key_value_pos], -1
        )

    # [bsz, q_len, num_heads, head_dim]
    query_states = query_states.view(
        bsz, q_len, self.num_heads, self.head_dim
    ).transpose(1, 2)
    key_states = key_states.view(
        bsz, q_len, self.num_key_value_heads, self.head_dim
    ).transpose(1, 2)
    value_states = value_states.view(
        bsz, q_len, self.num_key_value_heads, self.head_dim
    ).transpose(1, 2)

    if position_embeddings is None:
        cos, sin = self.rotary_emb(value_states, position_ids)
    else:
        cos, sin = position_embeddings

    if customized_rope_func is not None:  # eg, glm-4 rope
        query_states, key_states = customized_rope_func(
            query_states, key_states, cos, sin
        )
    else:
        query_states, key_states = apply_rotary_pos_emb(
            query_states, key_states, cos, sin
        )

    if past_key_value is not None:
        # sin and cos are specific to RoPE models; cache_position needed for the static cache
        cache_kwargs = {
            "sin": sin,
            "cos": cos,
            "cache_position": cache_position,
            "attn_forward_config": attn_forward_config,
            "attention_mask": attention_mask,
            "num_key_value_groups": self.num_key_value_groups,
            "query_states": query_states,
        }
        (
            key_states,
            value_states,
        ) = past_key_value.update(  # DynamicCache/KvcompressCache
            key_states,
            value_states,
            self.layer_idx,
            cache_kwargs,
        )
    key_states = repeat_kv(key_states, query_states.size(1) // key_states.size(1))
    value_states = repeat_kv(value_states, query_states.size(1) // value_states.size(1))

    dropout_rate = self.attention_dropout if self.training else 0.0

    if q_len == key_states.size(-2):  # prefilling
        assert q_len != 1
        if prefill_forward is not None:  # eg, a-shape/tri-shape/minference
            prefill_kwargs = {
                "attention_mask": attention_mask,
                "attn_forward_config": attn_forward_config,
            }
            attn_output = prefill_forward(  # [bsz, num_heads, q_len, head_dim]
                query_states,
                key_states,
                value_states,
                prefill_kwargs,
            )
            attn_output = attn_output.transpose(1, 2).contiguous()

        else:  # if not specified, use flash attention
            attn_output = _flash_attention_forward(  # [bsz, q_len, num_heads, head_dim]
                query_states.transpose(1, 2),
                key_states.transpose(1, 2),
                value_states.transpose(1, 2),
                attention_mask,
                q_len,
                position_ids=position_ids,
                dropout=dropout_rate,
                sliding_window=getattr(self, "sliding_window", None),
                is_causal=self.is_causal,
            )

    else:  # decoding
        assert q_len == 1
        if decoding_forward is not None:  # eg, llama-3.1
            decoding_kwargs = {
                "layer_idx": self.layer_idx,
                "attn_forward_config": attn_forward_config,
                "position_ids": position_ids,
            }
            attn_output = decoding_forward(
                query_states,
                key_states,
                value_states,
                decoding_kwargs,
            )
            attn_output = attn_output.transpose(1, 2)
        else:
            attn_output = _flash_attention_forward(
                query_states.transpose(1, 2),
                key_states.transpose(1, 2),
                value_states.transpose(1, 2),
                attention_mask,
                q_len,
                position_ids=position_ids,
                dropout=dropout_rate,
                sliding_window=getattr(self, "sliding_window", None),
                is_causal=self.is_causal,
            )

    attn_output = attn_output.reshape(bsz, q_len, -1).contiguous()
    attn_output = self.o_proj(attn_output)

    if not output_attentions:
        attn_weights = None

    return attn_output, attn_weights, past_key_value


prefill_forwards = {
    "dense": None,
    "a_shape": a_shape_kernel,
    "tri_shape": tri_shape_kernel,
}

decoding_forwards = {
    "dense": None,
    "snapkv": None,
    "pyramidkv": None,
    "quest": quest_decode_kernel,
    "streaming": None,
}
