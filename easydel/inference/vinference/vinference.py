# Copyright 2023 The EASYDEL Author @erfanzar (Erfan Zare Chavoshi).
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Module for text generation pipeline using JAX/Flax."""

import asyncio
import time
import warnings
from datetime import datetime
from functools import partial
from typing import AsyncGenerator, Dict, List, Optional, Union, overload
from uuid import uuid4

import flax.core
import jax
from fjformer import GenerateRNG, with_sharding_constraint
from fjformer.core import implicit_compact
from jax import lax
from jax import numpy as jnp
from jax import random as jrand
from jax.sharding import NamedSharding, PartitionSpec
from transformers import PreTrainedTokenizer

from easydel.etils.etils import get_logger
from easydel.inference.utils import (
	SampleState,
	inference_step_compiled,
	vInferenceConfig,
)
from easydel.inference.vinference.metrics import vInferenceMetrics
from easydel.modules.modeling_utils import EDPretrainedModel
from easydel.utils.compiling_utils import smart_compile

logger = get_logger(__name__)
TIME = str(datetime.fromtimestamp(time.time())).split(" ")[0]


def measure_flops(func, *args, **kwargs):
	start_time = time.perf_counter()
	flops = func.cost_analysis()[0]["flops"]
	result = func(*args, **kwargs)
	end_time = time.perf_counter()
	elapsed_time = end_time - start_time
	return result, flops, flops / elapsed_time, elapsed_time


@partial(jax.jit, static_argnames=["model", "generation_config"])
def _compiled_generate(
	model: EDPretrainedModel,
	params: dict,
	input_ids: jax.Array,
	attention_mask: jax.Array,
	position_ids: jax.Array,
	generation_config: vInferenceConfig,
	rng: jrand.PRNGKey,
) -> SampleState:
	"""
	Compiled function for performing the initial generation step.

	This function takes the model, parameters, input IDs, attention mask, position IDs,
	generation configuration, and a random number generator key as input. It initializes
	the generation state and performs the first sampling step.

	Args:
		model: The pre-trained language model.
		params: The model parameters.
		input_ids: The input token IDs.
		attention_mask: The attention mask.
		position_ids: The position IDs.
		generation_config: The generation configuration.
		rng: The random number generator key.

	Returns:
		SampleState: The initial generation state after the first sampling step.
	"""
	partition_axes = model.config.partition_axis
	mesh = model.config.mesh

	eos_token_id = jnp.array(generation_config.eos_token_id, dtype=jnp.int32)
	pad_token_id = jnp.array(generation_config.pad_token_id, dtype=jnp.int32)

	generation_spec = PartitionSpec(
		partition_axes.batch_axis,
		partition_axes.key_sequence_axis,
	)

	batch_size, current_length = input_ids.shape
	max_length = current_length + generation_config.max_new_tokens
	current_length = jnp.array(current_length)
	sequences = jnp.full((batch_size, max_length), pad_token_id, dtype=jnp.int32)
	sequences = lax.dynamic_update_slice(sequences, input_ids, (0, 0))
	is_sequence_finished = jnp.zeros((batch_size,), dtype=jnp.bool_)

	if attention_mask is None:
		warnings.warn(
			"`attention_mask` is not provided, it's recommended to "
			"pass an attention mask for better results.",
			stacklevel=1,
		)
		attention_mask = jnp.ones_like(input_ids)

	if position_ids is None:
		position_ids = attention_mask.cumsum(axis=-1, dtype="i4") - 1
	with mesh:
		input_ids = with_sharding_constraint(input_ids, generation_spec)
		attention_mask = with_sharding_constraint(attention_mask, generation_spec)
		position_ids = with_sharding_constraint(position_ids, generation_spec)
	assert (
		position_ids.shape == attention_mask.shape
	), "`position_ids` and `attention_mask` must have the same shape."

	state = SampleState(
		current_length=current_length,
		sequences=sequences,
		running_token=input_ids,
		is_sequence_finished=is_sequence_finished,
		prng_key=rng,
		model_kwargs=model.prepare_inputs_for_generation(
			input_ids=input_ids,
			max_length=max_length,
			attention_mask=attention_mask,
		),
		generated_tokens=0,
	)

	def cond_fn(state):
		"""state termination condition fn."""
		all_sequence_finished = jnp.all(state.is_sequence_finished)
		return ~jnp.logical_or(
			all_sequence_finished,
			state.current_length >= (current_length + generation_config.streaming_chunks),
		)

	@implicit_compact
	def sampling_step(params, state: SampleState):
		"""
		Performs a single sampling step for text generation.

		Args:
				params: Model parameters.
				state (inference_utils.SampleState): The current generation state.

		Returns:
				inference_utils.SampleState: The updated generation state.
		"""
		model_outputs = model(
			input_ids=state.running_token,
			params=params,
			add_params_field=True,
			return_dict=True,
			**state.model_kwargs,
		)
		next_token = inference_step_compiled(
			model_outputs.logits[:, -1],
			state.sequences,
			state.prng_key,
			generation_config,
			current_length,
			generation_config.max_new_tokens,
		)

		next_token = (
			next_token * ~state.is_sequence_finished
			+ pad_token_id * state.is_sequence_finished
		)

		next_sequence_finished = state.is_sequence_finished | jnp.isin(
			next_token,
			eos_token_id,
		)
		next_token = next_token[:, None]
		next_sequences = lax.dynamic_update_slice(
			state.sequences,
			next_token,
			(0, state.current_length),
		)
		next_model_kwargs = model.update_inputs_for_generation(
			model_outputs,
			state.model_kwargs,
		)

		return SampleState(
			current_length=state.current_length + 1,
			sequences=next_sequences,
			running_token=next_token,
			is_sequence_finished=next_sequence_finished,
			prng_key=jrand.split(state.prng_key, 2)[0],
			model_kwargs=next_model_kwargs,
			generated_tokens=state.generated_tokens + 1,
		)

	with mesh:
		if input_ids.shape[-1] > 1:
			state = sampling_step(params=params, state=state)
	return state


@partial(jax.jit, static_argnames=["model", "generation_config"])
def _compiled_interval_generate(
	model: EDPretrainedModel,
	params: dict,
	state: SampleState,
	generation_config: vInferenceConfig,
	loop_max_tokens: int,
	start_length: int,
) -> SampleState:
	"""
	Compiled function for performing interval generation steps.

	This function takes the model, parameters, current generation state, generation
	configuration, maximum number of tokens for the loop, and the starting length as input.
	It continues the generation process until the termination condition is met.

	Args:
		model: The pre-trained language model.
		params: The model parameters.
		state: The current generation state.
		generation_config: The generation configuration.
		loop_max_tokens: The maximum number of tokens to generate in the loop.
		start_length: The starting length of the input sequence.

	Returns:
		SampleState: The updated generation state after the interval generation steps.
	"""
	mesh = model.config.mesh

	eos_token_id = jnp.array(generation_config.eos_token_id, dtype=jnp.int32)
	pad_token_id = jnp.array(generation_config.pad_token_id, dtype=jnp.int32)
	tlen = state.current_length + loop_max_tokens

	def cond_fn(state):
		"""state termination condition fn."""
		all_sequence_finished = jnp.all(state.is_sequence_finished)
		return ~jnp.logical_or(all_sequence_finished, state.current_length >= tlen)

	@implicit_compact
	def sampling_step(params, state: SampleState):
		"""
		Performs a single sampling step for text generation.

		Args:
				params: Model parameters.
				state (inference_utils.SampleState): The current generation state.

		Returns:
				inference_utils.SampleState: The updated generation state.
		"""
		model_outputs = model(
			input_ids=state.running_token,
			params=params,
			add_params_field=True,
			return_dict=True,
			**state.model_kwargs,
		)
		next_token = inference_step_compiled(
			model_outputs.logits[:, -1],
			state.sequences,
			state.prng_key,
			generation_config,
			start_length,
			generation_config.max_new_tokens,
		)

		next_token = (
			next_token * ~state.is_sequence_finished
			+ pad_token_id * state.is_sequence_finished
		)

		next_sequence_finished = state.is_sequence_finished | jnp.isin(
			next_token,
			eos_token_id,
		)
		next_token = next_token[:, None]
		next_sequences = lax.dynamic_update_slice(
			state.sequences,
			next_token,
			(0, state.current_length),
		)
		next_model_kwargs = model.update_inputs_for_generation(
			model_outputs,
			state.model_kwargs,
		)

		return SampleState(
			current_length=state.current_length + 1,
			sequences=next_sequences,
			running_token=next_token,
			is_sequence_finished=next_sequence_finished,
			prng_key=jrand.split(state.prng_key, 2)[0],
			model_kwargs=next_model_kwargs,
			generated_tokens=state.generated_tokens + 1,
		)

	with mesh:

		def interval_sample(state):
			return sampling_step(params=params, state=state)

		state = jax.lax.while_loop(cond_fn, body_fun=interval_sample, init_val=state)
	return state


COMPILED_FUNCS = {}


def get_compiled_funcs(batch_size, input_tokens_length, id):
	"""
	Retrieves compiled generation functions from a cache.

	Args:
		batch_size: The batch size.
		input_tokens_length: The length of the input tokens.
		id: A unique identifier for the compilation.

	Returns:
		Tuple[Callable, Callable]: A tuple containing the compiled generate and
			interval generate functions, or (None, None) if not found in the cache.
	"""
	search_key = f"Bx{batch_size}-Sx{input_tokens_length}-UUID{id}"
	return COMPILED_FUNCS.get(search_key, (None, None))


def put_compiled_funcs(
	compiled_generate_func,
	compiled_interval_func,
	batch_size,
	input_tokens_length,
	id,
):
	"""
	Stores compiled generation functions in a cache.

	Args:
		compiled_generate_func: The compiled generate function.
		compiled_interval_func: The compiled interval generate function.
		batch_size: The batch size.
		input_tokens_length: The length of the input tokens.
		id: A unique identifier for the compilation.
	"""
	search_key = f"Bx{batch_size}-Sx{input_tokens_length}-UUID{id}"
	COMPILED_FUNCS[search_key] = (compiled_generate_func, compiled_interval_func)


class vInference:
	"""
	Class for performing text generation using a pre-trained language model in EasyDeL.

	This class handles the generation process, including initialization, precompilation,
	and generating text in streaming chunks.
	"""

	def __init__(
		self,
		model: EDPretrainedModel,
		params: Union[flax.core.FrozenDict, dict],
		tokenizer: PreTrainedTokenizer,
		generation_config: Optional[vInferenceConfig] = None,
		seed: Optional[int] = 42,
		input_partition_spec: Optional[PartitionSpec] = None,
		max_new_tokens: int = 512,
		inference_name: Optional[str] = None,
	):
		"""
		Initializes the vInference class.

		Args:
			model: The pre-trained language model.
			params: The model parameters.
			tokenizer: The tokenizer for the model.
			generation_config: The generation configuration.
			seed: The random seed for generation.
			input_partition_spec: The partitioning specification for input data.
			max_new_tokens: The maximum number of new tokens to generate.
		"""
		# fmt:off
		self.model = model
		self.params = self._validate_params(params) 
		self.tokenizer = tokenizer
		self.generation_config = self._init_generation_config(generation_config, max_new_tokens)
		self._rng_generator = GenerateRNG(seed)
		self.input_partition_spec = input_partition_spec or PartitionSpec(("dp", "fsdp"))
		self.mesh = self.model.config.mesh
		self._precompile_lock = asyncio.Lock()
		self._precompiled_configs = set()
		self._in_compiling_process = set()
		self._init_shardings()
		self._validate_token_ids()
		self._uuid4 = uuid4().hex 
		self._inference_name = inference_name or self._generate_inference_name(model)
		self.metrics = vInferenceMetrics(self._inference_name)
		# fmt:on

	def _generate_inference_name(self, model) -> str:
		"""
		Generate a standardized inference name combining model type, size, and timestamp.

		Format: {model_type}-{size_in_B}B-{timestamp}
		Example: llama-7.00B-20240311
		"""
		model_type = self._get_model_type(model)
		model_size = self._calculate_model_size(self.params)
		timestamp = datetime.now().strftime("%Y%m%d")

		return f"{model_type}-{model_size}B-{timestamp}"

	def _get_model_type(self, model) -> str:
		"""Get the model type, with fallback to 'unknown' if not found."""
		return getattr(model.config, "model_type", "unknown").lower()

	def _calculate_model_size(self, params) -> str:
		"""
		Calculate model size in billions of parameters.
		Returns formatted string with 2 decimal places.
		"""
		try:
			num_params = sum(
				n.size for n in jax.tree_util.tree_flatten(flax.core.unfreeze(params))[0]
			)
			size_in_billions = num_params / 1e9
			return f"{size_in_billions:.2f}"
		except Exception as e:
			logger.warning(f"Failed to calculate model size: {e}")
			return "unknown"

	@property
	def inference_name(self):
		return self._inference_name

	@property
	def model_prefill_length(self) -> int:
		"""
		Calculate the maximum length available for input prefill by subtracting
		the maximum new tokens from the model's maximum sequence length.

		Returns:
				int: The maximum length available for input prefill

		Raises:
				ValueError: If no maximum sequence length configuration is found
		"""
		possible_length_attributes = [
			"granted_mask_max_position_embedding",
			"max_position_embedding",
			"max_sequence_length",
		]

		max_length = self._get_model_max_length(possible_length_attributes)

		if max_length is None:
			raise ValueError(
				"Could not determine model's maximum sequence length. "
				f"Looked for attributes: {', '.join(possible_length_attributes)}"
			)

		return max_length - self.generation_config.max_new_tokens

	def _get_model_max_length(self, attributes: list[str]) -> Optional[int]:
		"""
		Find the first available maximum length configuration from a list of possible attributes.

		Args:
				attributes: List of attribute names to check in order of preference

		Returns:
				Optional[int]: The maximum length if found, None otherwise
		"""
		for attr in attributes:
			max_length = getattr(self.model.config, attr, None)
			if max_length is not None:
				return max_length
		return None

	def _validate_params(
		self, params: Union[flax.core.FrozenDict, dict]
	) -> Union[flax.core.FrozenDict, dict]:
		"""
		Validates the format of the model parameters.

		Args:
			params: The model parameters.

		Returns:
			Union[flax.core.FrozenDict, dict]: The validated model parameters.
		"""
		if "params" in params:
			warnings.warn(
				"`params` field should be like {k:v} not {'params':{k:v}}",
				DeprecationWarning,
				stacklevel=2,
			)
			return params["params"]
		return params

	def _init_generation_config(
		self, generation_config: Optional[vInferenceConfig], max_new_tokens: int
	) -> vInferenceConfig:
		"""
		Initializes the generation configuration.

		Args:
			generation_config: The generation configuration.
			max_new_tokens: The maximum number of new tokens to generate.

		Returns:
			vInferenceConfig: The initialized generation configuration.
		"""
		if generation_config is None:
			if self.model.generation_config is not None:
				return vInferenceConfig(
					bos_token_id=self.model.generation_config.bos_token_id,
					eos_token_id=self.model.generation_config.eos_token_id,
					pad_token_id=self.model.generation_config.pad_token_id,
					top_k=self.model.generation_config.top_k,
					top_p=self.model.generation_config.top_p,
					temperature=self.model.generation_config.temperature,
					max_new_tokens=self.model.generation_config.max_new_tokens or max_new_tokens,
				)
			return vInferenceConfig(max_new_tokens=max_new_tokens)
		return generation_config

	def _init_shardings(self):
		"""
		Initializes the shardings for input data.
		"""
		self.input_sharding = NamedSharding(
			spec=self.input_partition_spec,
			mesh=self.model.mesh,
		)
		self.empty_sharding = NamedSharding(
			spec=PartitionSpec(),
			mesh=self.model.mesh,
		)
		self.gen_input_sharding = NamedSharding(
			spec=PartitionSpec(self.input_partition_spec[0], None),
			mesh=self.model.mesh,
		)

	def _validate_token_ids(self):
		"""
		Validates the token IDs for padding, end-of-sequence, and beginning-of-sequence.
		"""
		if self.generation_config.pad_token_id is None:
			self.generation_config.pad_token_id = self.tokenizer.pad_token_id
		if self.generation_config.eos_token_id is None:
			self.generation_config.eos_token_id = self.tokenizer.eos_token_id
		if self.generation_config.bos_token_id is None:
			self.generation_config.bos_token_id = self.tokenizer.bos_token_id

		assert self.generation_config.pad_token_id is not None, (
			"`pad_token_id` cannot be None. "
			"(Set `tokenizer.pad_token_id = tokenizer.eos_token_id` if undefined)"
		)
		assert (
			self.generation_config.eos_token_id is not None
		), "`eos_token_id` cannot be None."

	async def generate(
		self,
		input_ids: jax.Array,
		attention_mask: Optional[jax.Array] = None,
		position_ids: Optional[jax.Array] = None,
	) -> AsyncGenerator[SampleState, None]:
		"""
		Generates text in streaming chunks.

		This function takes the input IDs, attention mask, and position IDs as input,
		precompiles the generation functions if necessary, and yields the generated text
		in streaming chunks.

		Args:
			input_ids: The input token IDs.
			attention_mask: The attention mask.
			position_ids: The position IDs.

		Yields:
			SampleState: The generated text in streaming chunks.
		"""
		self.metrics.queue_size.labels(model_name=self.metrics.model_name).inc()
		try:
			with self.metrics.inference_latency.labels(
				model_name=self.metrics.model_name,
				stage="preprocessing",
			).time():
				input_ids = jnp.array(input_ids)
				batch_size, seq_length = input_ids.shape
				_ = await self.async_precompile(batch_size, seq_length)
				generate_func, interval_func = get_compiled_funcs(
					batch_size=batch_size,
					input_tokens_length=seq_length,
					id=self._uuid4,
				)
				if attention_mask is None:
					warnings.warn(
						"`attention_mask` is not provided, it's recommended to "
						"pass an attention mask for better results.",
						stacklevel=1,
					)
					attention_mask = jnp.ones_like(input_ids)

				if position_ids is None:
					position_ids = attention_mask.cumsum(axis=-1, dtype="i4") - 1

				attention_mask = jnp.array(attention_mask)
				start_length = input_ids.shape[-1]
			with self.metrics.inference_latency.labels(
				model_name=self.metrics.model_name,
				stage="inference",
			).time():
				state, flop, generate_func_flops, generate_func_elapsed_time = measure_flops(
					generate_func,
					params=self.params,
					input_ids=input_ids,
					attention_mask=attention_mask,
					position_ids=position_ids,
					rng=self._rng_generator.rng,
				)
				state.generate_func_flops = generate_func_flops
				if not state.is_sequence_finished:
					all_interval_func_flops = []
					for _ in range(self.generation_config._loop_rows):
						state, flop, interval_func_flops, interval_func_elapsed_time = (
							measure_flops(
								interval_func,
								params=self.params,
								state=state,
								loop_max_tokens=self.generation_config.streaming_chunks,
								start_length=start_length,
							)
						)
						all_interval_func_flops.append(interval_func_flops)
						interval_func_flops = sum(all_interval_func_flops) / len(
							all_interval_func_flops
						)
						state.generate_func_flops = generate_func_flops
						state.interval_func_flops = interval_func_flops
						yield state
						if state.is_sequence_finished:
							break
				else:
					yield state
			self.metrics.inference_requests.labels(
				model_name=self.metrics.model_name,
				status="success",
			).inc()
		except Exception as e:
			self.metrics.inference_requests.labels(
				model_name=self.metrics.model_name,
				status="error",
			).inc()
			raise e
		finally:
			self.metrics.queue_size.labels(model_name=self.metrics.model_name).dec()
			self.metrics.token_throughput.labels(
				model_name=self.metrics.model_name,
				operation="output",
			).inc(state.generated_tokens)
			self.metrics.generation_length.labels(
				model_name=self.metrics.model_name,
			).observe(state.generated_tokens)

	def _compile_and_lower_funs(self, batch_size: int, input_tokens_length: int):
		compiled_generate_func, compiled_interval_func = get_compiled_funcs(
			batch_size=batch_size,
			input_tokens_length=input_tokens_length,
			id=self._uuid4,
		)
		do_compile = compiled_generate_func is None or compiled_interval_func is None
		if do_compile:
			input_ids = jnp.ones((batch_size, input_tokens_length), dtype="i4")
			attention_mask = jnp.ones_like(input_ids)
			position_ids = attention_mask.cumsum(axis=-1, dtype="i4") - 1

			compiled_generate_func = smart_compile(
				_compiled_generate.lower(
					model=self.model,
					params=self.params,
					input_ids=input_ids,
					attention_mask=attention_mask,
					position_ids=position_ids,
					generation_config=self.generation_config,
					rng=self._rng_generator.rng,
				),
				tag="vinference",
			)
			state = compiled_generate_func(
				params=self.params,
				input_ids=input_ids,
				attention_mask=attention_mask,
				position_ids=position_ids,
				rng=self._rng_generator.rng,
			)
			compiled_interval_func = smart_compile(
				_compiled_interval_generate.lower(
					model=self.model,
					params=self.params,
					state=state,
					generation_config=self.generation_config,
					loop_max_tokens=self.generation_config.streaming_chunks,
					start_length=input_tokens_length,
				),
				tag="vinference",
			)
			del state
			put_compiled_funcs(
				compiled_generate_func=compiled_generate_func,
				compiled_interval_func=compiled_interval_func,
				batch_size=batch_size,
				input_tokens_length=input_tokens_length,
				id=self._uuid4,
			)

	async def async_precompile(
		self,
		batch_size: int,
		input_tokens_length: Optional[int] = None,
	):
		"""
		Precompiles the generation functions for a given batch size and input length.

		This function checks if the generation functions have already been compiled for
		the given configuration. If not, it compiles them asynchronously and stores them
		in a cache.

		Args:
			batch_size: The batch size.
			input_tokens_length: The length of the input tokens.

		Returns:
			bool: True if precompilation was successful, False otherwise.
		"""
		if input_tokens_length is None:
			input_tokens_length = self.model_prefill_length
		config_key = (batch_size, input_tokens_length)
		if config_key in self._precompiled_configs:
			return True
		async with self._precompile_lock:
			if config_key in self._precompiled_configs:
				return True
			if config_key in self._in_compiling_process:
				await asyncio.sleep(5)
				return await self.async_precompile(
					batch_size=batch_size,
					input_tokens_length=input_tokens_length,
				)
			else:
				with self.metrics.compilation_time.labels(
					model_name=self.metrics.model_name,
					function_name="_compile_and_lower_funs",
				).time():
					self._in_compiling_process.add(config_key)
					self._compile_and_lower_funs(
						batch_size=batch_size,
						input_tokens_length=input_tokens_length,
					)
					self._precompiled_configs.add(config_key)
			return True

	def precompile(
		self,
		batch_size: int,
		input_tokens_length: Optional[int] = None,
	):
		"""
		Precompiles the generation functions for a given batch size and input length.

		This function checks if the generation functions have already been compiled for
		the given configuration. If not, it compiles them asynchronously and stores them
		in a cache.

		Args:
			batch_size: The batch size.
			input_tokens_length: The length of the input tokens.

		Returns:
			bool: True if precompilation was successful, False otherwise.
		"""
		if input_tokens_length is None:
			input_tokens_length = self.model_prefill_length
		config_key = (batch_size, input_tokens_length)

		if config_key in self._precompiled_configs:
			return True
		if config_key in self._in_compiling_process:
			time.sleep(5)
			return self.precompile(
				batch_size=batch_size,
				input_tokens_length=input_tokens_length,
			)
		else:
			with self.metrics.compilation_time.labels(
				model_name=self.metrics.model_name,
				function_name="_compile_and_lower_funs",
			).time():
				self._in_compiling_process.add(config_key)
				self._compile_and_lower_funs(
					batch_size=batch_size,
					input_tokens_length=input_tokens_length,
				)
				self._precompiled_configs.add(config_key)
		return True

	@overload
	async def count_tokens(self, messages: List[Dict[str, str]]): ...
	@overload
	async def count_tokens(self, text: str): ...

	async def count_tokens(self, conv: Union[str, List[Dict[str, str]]]) -> int:
		if isinstance(conv, list) and all(isinstance(item, dict) for item in conv):
			# Handle chat messages using chat template
			tokens = self.tokenizer.apply_chat_template(
				conv,
				tokenize=True,
				apply_chat_template=True,
			)
			return len(tokens)
		else:
			tokens = self.tokenizer.encode(conv)
			return len(tokens)
