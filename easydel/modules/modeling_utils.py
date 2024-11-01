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

import os
import re
import warnings
from copy import deepcopy
from dataclasses import dataclass
from typing import (
	Any,
	Callable,
	List,
	Literal,
	Mapping,
	Optional,
	Sequence,
	Tuple,
	Union,
)

import chex
import fjformer
import fjformer.sharding
import flax
import flax.linen
import jax
import jax.extend
import jax.tree_util
from fjformer.checkpoint import CheckpointManager
from fjformer.dtypes import Array8Bit
from fjformer.sharding import match_partition_rules
from flax.core import FrozenDict, unfreeze
from flax.traverse_util import flatten_dict, unflatten_dict
from jax import numpy as jnp
from jax.experimental.mesh_utils import create_device_mesh
from jax.sharding import Mesh, PartitionSpec
from tqdm.auto import tqdm
from transformers.configuration_utils import PretrainedConfig
from transformers.modeling_flax_utils import FlaxPreTrainedModel
from transformers.utils.generic import working_or_temp_dir
from easydel.etils.easystate import EasyDeLState
from easydel.etils.etils import (
	AVAILABLE_ATTENTION_MECHANISMS,
	DEFAULT_ATTENTION_MECHANISM,
	get_logger,
)
from easydel.etils.partition_module import PartitionAxis
from easydel.utils.quantizers import DEFAULT_QUANTIZATION_PATTERN, EasyQuantizer

logger = get_logger(__name__)

FLAX_WEIGHTS_NAME = "easydel-model.parameters"

DEFAULT_PALLAS_M_BLOCK_SIZE = 128
DEFAULT_PALLAS_K_BLOCK_SIZE = 128
DEFAULT_PALLAS_N_BLOCK_SIZE = 128
DEFAULT_HARDWARE_ABSTRACTION = False
ED_DEFAULT_HARDWARE_ABSTRACTION = os.environ.get(
	"ED_DEFAULT_HARDWARE_ABSTRACTION",
	default="false",
).lower() in ["true", "1", "yes"]

EKERNEL_OPS = os.environ.get(
	"EKERNEL_OPS",
	default="false",
).lower() in ["true", "1", "yes"]

if ED_DEFAULT_HARDWARE_ABSTRACTION:
	DEFAULT_HARDWARE_ABSTRACTION = True


if jax.extend.backend.get_backend().platform == "tpu":
	DEFAULT_PALLAS_M_BLOCK_SIZE = None  # Autoset
	DEFAULT_PALLAS_K_BLOCK_SIZE = None  # Autoset
	DEFAULT_PALLAS_N_BLOCK_SIZE = None  # Autoset

if DEFAULT_HARDWARE_ABSTRACTION:
	logger.info("HARDWARE_ABSTRACTION is ON by default")

if EKERNEL_OPS:
	logger.info(
		"`EKERNEL_OPS` is ON and some operations will "
		"automatically be replaced by EasyDeL."
	)
	from easydel.kernels.gemm import replace_dot_general_with_gemm

	replace_dot_general_with_gemm()


def set_attrs_smartly(self, attr_name: str, default: Any, new_attr: Any):
	if not hasattr(self, attr_name):
		setattr(self, attr_name, default)
	if not new_attr == Ellipsis:
		setattr(self, attr_name, new_attr)


@dataclass
class EasyMethod:
	TRAIN: str = "train"
	SERVE: str = "serve"
	EVAL: str = "serve"
	CONVERT: str = "convert"


warnings.filterwarnings(
	"ignore",
	message="Passing `gradient_checkpointing` to a config initialization is deprecated",  # EasyDeL handle will this
)


warnings.filterwarnings("ignore", message="You are using a model of type")


class EDPretrainedConfig(PretrainedConfig):
	"""It initializes all the attributes of an object, and it's called when you create a new instance of that class.

	Args:
	    axis_dims (Sequence[int]): Specify the number of dimensions for each axis
	    axis_names (Sequence[str]): Set the names of the axes
	    attn_mechanism (AVAILABLE_ATTENTION_MECHANISMS): attention mechanism to use
	    block_k (int): block size of key_states
	    block_q (int): block size of query_states
	    block_b (int): block size of bias
	    partition_axis (PartitionAxis) : PartitionAxis is new module used for partitioning arrays in easydel.
	    shard_attention_computation (bool): whenever to shard qkv b for attention
	    use_sharding_constraint (bool): whether to use sharding constraint for the arrays
	    use_scan_mlp (bool): Determine whether to use scan_mlp or not
	    backend (Optional[Literal["cpu", "gpu", "tpu"]]): Specify the backen
	    platform (Optional[Literal["jax", "triton", "pallas"]]): Specify the platform to used to use
	    flash_attention_backward_pass_impl (Literal["triton", "xla"]): Specify the backward pass kernel for flash attention
	    attn_dtype (jnp.dtype): data type for computing attention.
	    fcm_max_ratio (float): value for fcm mask - max ratio
	    fcm_min_ratio (float): value for fcm mask - min ratio
			hardware_abstraction (bool): whenever to switch to custom pallas kernels instead of JAX
			pallas_m_block_size (int): block size m dim in matmul for pallas kernel `A(mk)@B(kn)=B(mn)`.
			pallas_k_block_size (int): block size k dim in matmul for pallas kernel `A(mk)@B(kn)=B(mn)`.
			pallas_n_block_size (int): block size n dim in matmul for pallas kernel `A(mk)@B(kn)=B(mn)`.
	"""

	_show_private_attrs: bool = False

	def __init__(
		self,
		axis_dims: Sequence[int] = (1, -1, 1, 1),
		axis_names: Sequence[str] = ("dp", "fsdp", "tp", "sp"),
		attn_mechanism: AVAILABLE_ATTENTION_MECHANISMS = DEFAULT_ATTENTION_MECHANISM,
		block_k: int = 128,
		block_q: int = 128,
		block_b: int = 1,
		partition_axis: PartitionAxis = PartitionAxis(),  # noqa
		shard_attention_computation: bool = True,
		use_sharded_kv_caching: bool = True,
		use_sharding_constraint: bool = False,
		backend: Optional[Literal["cpu", "gpu", "tpu"]] = None,
		platform: Optional[Literal["jax", "triton", "pallas"]] = None,
		easy_method: Literal["train", "serve", "convert"] = EasyMethod.TRAIN,
		bits: Optional[int] = None,
		scan_ring_attention: bool = True,
		scan_attention_layers: bool = False,
		use_scan_mlp: bool = False,
		scan_mlp_chunk_size: int = 1024,
		attention_axis_name: str = "sp",
		quantize_kv_cache: bool = False,
		flash_attention_backward_pass_impl: Literal["triton", "xla"] = "triton",
		attn_dtype: jnp.dtype = jnp.float32,
		fcm_max_ratio: float = 0.0,
		fcm_min_ratio: float = 0.0,
		hardware_abstraction: bool = DEFAULT_HARDWARE_ABSTRACTION,
		pallas_m_block_size: int = DEFAULT_PALLAS_M_BLOCK_SIZE,
		pallas_k_block_size: int = DEFAULT_PALLAS_K_BLOCK_SIZE,
		pallas_n_block_size: int = DEFAULT_PALLAS_N_BLOCK_SIZE,
		**kwargs,
	):
		self.axis_dims = getattr(
			self,
			"axis_dims",
			axis_dims,
		)
		self.axis_names = getattr(
			self,
			"axis_names",
			axis_names,
		)
		self.backend = getattr(
			self,
			"backend",
			backend if backend is not None else jax.default_backend(),
		)
		self.platform = getattr(
			self,
			"platform",
			platform
			if platform is not None
			else ("triton" if jax.default_backend() == "gpu" else "jax"),
		)
		self.easy_method = getattr(
			self,
			"easy_method",
			easy_method,
		)
		self.attn_mechanism = getattr(
			self,
			"attn_mechanism",
			attn_mechanism,
		)
		self.block_b = getattr(
			self,
			"block_b",
			block_b,
		)
		self.block_k = getattr(
			self,
			"block_k",
			block_k,
		)
		self.block_q = getattr(
			self,
			"block_q",
			block_q,
		)
		self.partition_axis = getattr(
			self,
			"partition_axis",
			partition_axis,
		)
		self.shard_attention_computation = getattr(
			self,
			"shard_attention_computation",
			shard_attention_computation,
		)
		self.bits = getattr(
			self,
			"bits",
			bits,
		)
		self.scan_attention_layers = getattr(
			self,
			"scan_attention_layers",
			scan_attention_layers,
		)
		self.scan_ring_attention = getattr(
			self,
			"scan_ring_attention",
			scan_ring_attention,
		)
		self.use_sharded_kv_caching = getattr(
			self,
			"use_sharded_kv_caching",
			use_sharded_kv_caching,
		)
		self.use_scan_mlp = getattr(
			self,
			"use_scan_mlp",
			use_scan_mlp,
		)
		self.scan_mlp_chunk_size = getattr(
			self,
			"scan_mlp_chunk_size",
			scan_mlp_chunk_size,
		)
		self.use_sharding_constraint = getattr(
			self,
			"use_sharding_constraint",
			use_sharding_constraint,
		)
		self.attention_axis_name = getattr(
			self,
			"attention_axis_name",
			attention_axis_name,
		)
		self.quantize_kv_cache = getattr(
			self,
			"quantize_kv_cache",
			quantize_kv_cache,
		)
		self.flash_attention_backward_pass_impl = getattr(
			self,
			"flash_attention_backward_pass_impl",
			flash_attention_backward_pass_impl,
		)
		self.attn_dtype = getattr(
			self,
			"attn_dtype",
			attn_dtype,
		)

		self.fcm_max_ratio = getattr(
			self,
			"fcm_max_ratio",
			fcm_max_ratio,
		)
		self.fcm_min_ratio = getattr(
			self,
			"fcm_min_ratio",
			fcm_min_ratio,
		)

		self.hardware_abstraction = getattr(
			self,
			"hardware_abstraction",
			hardware_abstraction,
		)
		self.pallas_m_block_size = getattr(
			self,
			"pallas_m_block_size",
			pallas_m_block_size,
		)
		self.pallas_k_block_size = getattr(
			self,
			"pallas_k_block_size",
			pallas_k_block_size,
		)
		self.pallas_n_block_size = getattr(
			self,
			"pallas_n_block_size",
			pallas_n_block_size,
		)

		self.pretraining_tp = 1  # it's for pytorch models.
		if self.quantize_kv_cache and self.use_sharded_kv_caching:
			quantize_kv_cache = self.quantize_kv_cache
			use_sharded_kv_caching = self.use_sharded_kv_caching
			logger.warn(
				f"`{quantize_kv_cache=}` and `{use_sharded_kv_caching=}`"
				" can't be used together at the moment."
			)
		super().__init__(**kwargs)

	@staticmethod
	def create_mesh(
		axis_dims: Sequence[int] = (1, -1, 1, 1),
		axis_names: Sequence[str] = ("dp", "fsdp", "tp", "sp"),
		backend="",
	):
		"""The create_mesh function creates a mesh object that can be used to shard arrays.

		Args:
		    axis_dims: Sequence[int]: Specify the dimensions of the mesh
		    axis_names: Sequence[str]: Name the axes of the mesh
		    backend: Specify the backend to use

		Returns:
		    A mesh object
		"""
		array_devices = jax.numpy.ones(
			(len(jax.devices() if backend == "" else jax.devices(backend)), 1)
		)
		if isinstance(axis_dims, str):
			axis_dims = eval(axis_dims)
			warnings.warn(
				"axis_dims argument is not a Sequence of int and it's an string. "
				"(backbone Warning in EasyDeLModuleConfig)\n"
				f"\tchanged to {axis_dims}",
				stacklevel=1,
			)
		if isinstance(axis_names, str):
			axis_names = eval(axis_names)
			warnings.warn(
				"axis_names argument is not a Sequence of strings and it's an string class. "
				"(backbone Warning in EasyDeLModuleConfig)\n"
				f"\tchanged to {axis_names}",
				stacklevel=1,
			)
		resh = array_devices.reshape(axis_dims).shape

		return Mesh(create_device_mesh(resh), axis_names)

	@property
	def mesh(self):
		"""The mesh property is a helper property that creates a Mesh object from the
		axis_dims and axis_names attributes of an object, which are assumed to be lists of integers and strings, respectively.
		The platform attribute is also used if it exists.

		Args:
		    self: Refer to the object itself

		Returns:
		    A jaxMesh
		"""
		return self.create_mesh(
			axis_dims=(
				[v for k, v in self.axis_dims.items()]
				if isinstance(self.axis_dims, dict)
				else self.axis_dims
			),
			axis_names=(
				[v for k, v in self.axis_names.items()]
				if isinstance(self.axis_names, dict)
				else self.axis_names
			),
			backend=(
				(self.backend if self.backend is not None else "")
				if hasattr(self, "backend")
				else ""
			),
		)

	def jax_mesh(self):
		warnings.warn("`jax_mesh` is deprecated use `get_mesh` or `mesh`", stacklevel=1)
		return self.get_mesh()

	def get_partition_rules(self, fully_sharded_data_parallel: bool = True):
		"""
		Get the partition rules for the model.

		Args:
		    fully_sharded_data_parallel (`bool`, *optional*, defaults to `True`):
		        Whether to use fully sharded data parallelism.

		Returns:
		    `Tuple[Tuple[str, PartitionSpec]]`: The partition rules.
		"""
		if not fully_sharded_data_parallel:
			raise NotImplementedError()
		else:
			return (
				(
					".*",
					PartitionSpec(
						("fsdp", "sp"),
					),
				),
			)

	def get_axis_dims(self) -> Sequence[int]:
		"""The get_axis_dims function returns a sequence of integers representing the dimensions of each axis.

		Args:
		    self: Represent the instance of the class

		Returns:
		    The dimensions of the axes
		"""
		return self.axis_dims

	def get_axis_names(self) -> Sequence[str]:
		"""The get_axis_names function returns a list of the names of the axes.

		Args:
		    self: Represent the instance of the class

		Returns:
		    A list of the names of all axes
		"""
		return self.axis_names

	def get_backend(self) -> str:
		"""The get_backend function returns the backend that is currently being used.
		If no backend has been set, it will return the default JAX backend.

		Args:
		    self: Bind the method to an object

		Returns:
		    The backend platform
		"""
		return (
			self.backend
			if not self.backend == ""
			else jax.extend.backend.get_backend().platform
		)

	def add_basic_configurations(
		self,
		axis_dims: Sequence[int] = ...,
		axis_names: Sequence[str] = ...,
		attn_mechanism: AVAILABLE_ATTENTION_MECHANISMS = ...,
		block_k: int = ...,
		block_q: int = ...,
		block_b: int = ...,
		partition_axis: PartitionAxis = ...,
		shard_attention_computation: bool = ...,
		use_sharded_kv_caching: bool = ...,
		backend: Optional[Literal["cpu", "gpu", "tpu"]] = ...,
		platform: Optional[Literal["jax", "triton", "pallas"]] = ...,
		easy_method: Literal["train", "serve", "convert"] = ...,
		bits: Optional[int] = ...,
		scan_ring_attention: bool = ...,
		scan_attention_layers: bool = ...,
		use_sharding_constraint: bool = ...,
		use_scan_mlp: bool = ...,
		scan_mlp_chunk_size: int = ...,
		attention_axis_name: str = ...,
		quantize_kv_cache: bool = ...,
		flash_attention_backward_pass_impl: Literal["triton", "xla"] = ...,
		attn_dtype: jnp.dtype = ...,
		hardware_abstraction: bool = ...,
		pallas_m_block_size: int = ...,
		pallas_k_block_size: int = ...,
		pallas_n_block_size: int = ...,
	):
		"""
		It initializes all the attributes of an object, and it's called when you create a new instance of that class.

		Args:
		    axis_dims (Sequence[int]): Specify the number of dimensions for each axis
		    axis_names (Sequence[str]): Set the names of the axes
		    attn_mechanism (AVAILABLE_ATTENTION_MECHANISMS): attention mechanism to use
		    block_k (int): block size of key_states
		    block_q (int): block size of query_states
		    block_b (int): block size of bias
		    partition_axis (PartitionAxis) : PartitionAxis is new module used for partitioning arrays in easydel.
		    shard_attention_computation (bool): whenever to use shard_map for attention
		    use_sharded_kv_caching (bool): whenever to use shard_map and sharding for key and value
		    backend (Optional[Literal["cpu", "gpu", "tpu"]]): Specify the backend to use
		platform (Optional[Literal["jax", "triton", "pallas"]]): Specify the platform to used to use
		    easy_method (Literal["train", "serve", "convert"]): easydel Quantization Method to be applied for
		    bits (Optional[int]): Model bits for quantization
		    use_sharding_constraint (bool): whether to use sharding constraint for the arrays
		    scan_ring_attention (bool): Whether to use can for ring attention
		    scan_attention_layers (bool): Whether to use can for attention layers
		    use_scan_mlp (bool): Determine whether to use scan_mlp or not
		    scan_mlp_chunk_size (int): Size of chunks in scan MLP.
		    attention_axis_name (str): Name of the attention axis name
		    quantize_kv_cache (bool): Whether to quantize Key/Value in attention for generation process.
		    flash_attention_backward_pass_impl (Literal["triton", "xla"]): Specify the backward pass kernel for flash attention
				hardware_abstraction (bool): whenever to switch to custom pallas kernels instead of JAX.
				pallas_m_block_size (int): block size m dim in matmul for pallas kernel `A(mk)@B(kn)=B(mn)`.
				pallas_k_block_size (int): block size k dim in matmul for pallas kernel `A(mk)@B(kn)=B(mn)`.
				pallas_n_block_size (int): block size n dim in matmul for pallas kernel `A(mk)@B(kn)=B(mn)`.
		"""
		set_attrs_smartly(
			self,
			"axis_dims",
			(1, -1, 1, 1),
			axis_dims,
		)
		set_attrs_smartly(
			self,
			"axis_names",
			("dp", "fsdp", "tp", "sp"),
			axis_names,
		)

		set_attrs_smartly(
			self,
			"block_q",
			512,
			block_q,
		)
		set_attrs_smartly(
			self,
			"block_k",
			512,
			block_k,
		)
		set_attrs_smartly(
			self,
			"block_b",
			1,
			block_b,
		)

		set_attrs_smartly(
			self,
			"partition_axis",
			PartitionAxis(),
			partition_axis,
		)
		set_attrs_smartly(
			self,
			"use_sharding_constraint",
			False,
			use_sharding_constraint,
		)
		set_attrs_smartly(
			self,
			"backend",
			None,
			backend,
		)
		set_attrs_smartly(
			self,
			"platform",
			"jax",
			platform,
		)
		set_attrs_smartly(
			self,
			"shard_attention_computation",
			True,
			shard_attention_computation,
		)
		set_attrs_smartly(
			self,
			"use_sharded_kv_caching",
			True,
			use_sharded_kv_caching,
		)
		set_attrs_smartly(
			self,
			"attn_mechanism",
			"jax_flash_attn2",
			attn_mechanism,
		)

		set_attrs_smartly(
			self,
			"easy_method",
			EasyMethod.TRAIN,
			easy_method,
		)
		set_attrs_smartly(
			self,
			"bits",
			None,
			bits,
		)
		set_attrs_smartly(
			self,
			"scan_attention_layers",
			True,
			scan_attention_layers,
		)
		set_attrs_smartly(
			self,
			"scan_ring_attention",
			True,
			scan_ring_attention,
		)
		set_attrs_smartly(
			self,
			"use_scan_mlp",
			False,
			use_scan_mlp,
		)
		set_attrs_smartly(
			self,
			"scan_mlp_chunk_size",
			1024,
			scan_mlp_chunk_size,
		)
		set_attrs_smartly(
			self,
			"attention_axis_name",
			"sp",
			attention_axis_name,
		)
		set_attrs_smartly(
			self,
			"quantize_kv_cache",
			False,
			quantize_kv_cache,
		)
		set_attrs_smartly(
			self,
			"flash_attention_backward_pass_impl",
			"triton",
			flash_attention_backward_pass_impl,
		)

		set_attrs_smartly(
			self,
			"attn_dtype",
			jnp.float32,
			attn_dtype,
		)

		set_attrs_smartly(
			self,
			"hardware_abstraction",
			DEFAULT_HARDWARE_ABSTRACTION,
			hardware_abstraction,
		)

		set_attrs_smartly(
			self,
			"pallas_m_block_size",
			DEFAULT_PALLAS_M_BLOCK_SIZE,
			pallas_m_block_size,
		)

		set_attrs_smartly(
			self,
			"pallas_k_block_size",
			DEFAULT_PALLAS_K_BLOCK_SIZE,
			pallas_k_block_size,
		)

		set_attrs_smartly(
			self,
			"pallas_n_block_size",
			DEFAULT_PALLAS_N_BLOCK_SIZE,
			pallas_n_block_size,
		)

	def __repr__(self):
		"""The __repr__ function is used to generate a string representation of an object.
		This function should return a string that can be parsed by the Python interpreter
		to recreate the object. The __repr__ function is called when you use print() on an
		object, or when you type its name in the REPL.

		Args:
		    self: Refer to the instance of the class

		Returns:
		    A string representation of the object
		"""
		from easydel.etils.easystate import TYPE_SEP, VALUE_SEP

		string = f"{self.__class__.__name__}(\n"
		for k, v in self.__dict__.items():
			if not self._show_private_attrs and VALUE_SEP in k and TYPE_SEP in k:
				continue
			if not k.startswith("_"):
				try:
					repr_src = f"  {k} : " + v.__str__().replace("\n", "\n  ") + "\n"
					string += (
						repr_src
						if len(repr_src) < 500
						else f"  {k} : " + f"{v.__class__.__name__}(...)" + "\n"
					)
				except TypeError:
					pass
		return string + ")"

	def add_jax_args(self, **kwargs):
		for k, v in kwargs.items():
			set_attrs_smartly(self, k, v, v)

	def __str__(self):
		"""The __str__ function is called when you use the print function or when str() is used.
		It should return a string representation of the object.

		Args:
		    self: Refer to the instance of the class

		Returns:
		    The object's string representation
		"""
		return self.__repr__()

	@classmethod  # From HF.
	def from_pretrained(
		cls,
		pretrained_model_name_or_path: Union[str, os.PathLike],
		cache_dir: Optional[Union[str, os.PathLike]] = None,
		force_download: bool = False,
		local_files_only: bool = False,
		token: Optional[Union[str, bool]] = None,
		revision: str = "main",
		**kwargs,
	) -> "PretrainedConfig":
		r"""
		Instantiate a [`PretrainedConfig`] (or a derived class) from a pretrained model configuration.

		Args:
				pretrained_model_name_or_path (`str` or `os.PathLike`):
						This can be either:

						- a string, the *model id* of a pretrained model configuration hosted inside a model repo on
							huggingface.co.
						- a path to a *directory* containing a configuration file saved using the
							[`~PretrainedConfig.save_pretrained`] method, e.g., `./my_model_directory/`.
						- a path or url to a saved configuration JSON *file*, e.g., `./my_model_directory/configuration.json`.
				cache_dir (`str` or `os.PathLike`, *optional*):
						Path to a directory in which a downloaded pretrained model configuration should be cached if the
						standard cache should not be used.
				force_download (`bool`, *optional*, defaults to `False`):
						Whether or not to force to (re-)download the configuration files and override the cached versions if
						they exist.
				resume_download:
						Deprecated and ignored. All downloads are now resumed by default when possible.
						Will be removed in v5 of Transformers.
				proxies (`Dict[str, str]`, *optional*):
						A dictionary of proxy servers to use by protocol or endpoint, e.g., `{'http': 'foo.bar:3128',
						'http://hostname': 'foo.bar:4012'}.` The proxies are used on each request.
				token (`str` or `bool`, *optional*):
						The token to use as HTTP bearer authorization for remote files. If `True`, or not specified, will use
						the token generated when running `huggingface-cli login` (stored in `~/.huggingface`).
				revision (`str`, *optional*, defaults to `"main"`):
						The specific model version to use. It can be a branch name, a tag name, or a commit id, since we use a
						git-based system for storing models and other artifacts on huggingface.co, so `revision` can be any
						identifier allowed by git.

						<Tip>

						To test a pull request you made on the Hub, you can pass `revision="refs/pr/<pr_number>".

						</Tip>

				return_unused_kwargs (`bool`, *optional*, defaults to `False`):
						If `False`, then this function returns just the final configuration object.

						If `True`, then this functions returns a `Tuple(config, unused_kwargs)` where *unused_kwargs* is a
						dictionary consisting of the key/value pairs whose keys are not configuration attributes: i.e., the
						part of `kwargs` which has not been used to update `config` and is otherwise ignored.
				subfolder (`str`, *optional*, defaults to `""`):
						In case the relevant files are located inside a subfolder of the model repo on huggingface.co, you can
						specify the folder name here.
				kwargs (`Dict[str, Any]`, *optional*):
						The values in kwargs of any keys which are configuration attributes will be used to override the loaded
						values. Behavior concerning key/value pairs whose keys are *not* configuration attributes is controlled
						by the `return_unused_kwargs` keyword parameter.

		Returns:
				[`PretrainedConfig`]: The configuration object instantiated from this pretrained model.

		Examples:

		```python
		# We can't instantiate directly the base class *PretrainedConfig* so let's show the examples on a
		# derived class: BertConfig
		config = BertConfig.from_pretrained(
		  "google-bert/bert-base-uncased"
		)  # Download configuration from huggingface.co and cache.
		config = BertConfig.from_pretrained(
		  "./test/saved_model/"
		)  # E.g. config (or model) was saved using *save_pretrained('./test/saved_model/')*
		config = BertConfig.from_pretrained("./test/saved_model/my_configuration.json")
		config = BertConfig.from_pretrained(
		  "google-bert/bert-base-uncased", output_attentions=True, foo=False
		)
		assert config.output_attentions == True
		config, unused_kwargs = BertConfig.from_pretrained(
		  "google-bert/bert-base-uncased",
		  output_attentions=True,
		  foo=False,
		  return_unused_kwargs=True,
		)
		assert config.output_attentions == True
		assert unused_kwargs == {"foo": False}
		```"""
		kwargs["cache_dir"] = cache_dir
		kwargs["force_download"] = force_download
		kwargs["local_files_only"] = local_files_only
		kwargs["revision"] = revision

		cls._set_token_in_kwargs(kwargs, token)

		config_dict, kwargs = cls.get_config_dict(pretrained_model_name_or_path, **kwargs)

		return cls.from_dict(config_dict, **kwargs)


class EDPretrainedModel(FlaxPreTrainedModel):
	def __init__(
		self,
		config: Optional[PretrainedConfig] = None,
		module: Optional[flax.linen.Module] = None,
		input_shape: Tuple = (1, 1),
		seed: int = 0,
		dtype: jnp.dtype = jnp.float32,
		param_dtype: jnp.dtype = jnp.float32,  # Ignored
		precision: Optional[Union[jax.lax.Precision, str]] = None,  # Ignored
		_do_init: bool = True,
	):
		assert config is not None, "`config` must be provided.`"
		assert module is not None, "`module` must be provided.`"
		super().__init__(
			config=config,
			module=module,
			input_shape=input_shape,
			seed=seed,
			dtype=dtype,
			_do_init=_do_init,
		)

	@property
	def mesh(self):
		return self.config.mesh

	def get_named_sharding(self, partition_rules=None, partition_specs=None):
		if partition_rules is None:
			partition_rules = self.config.get_partition_rules(True)
		if partition_specs is None:
			partition_specs = match_partition_rules(partition_rules, self.params_shape_tree)
		return jax.tree_util.tree_map(
			lambda spec: jax.sharding.NamedSharding(
				spec=spec,
				mesh=self.mesh,
			),
			partition_specs,
		)

	def get_input_embeddings(self):
		"""The get_input_embeddings function returns the embedding layer of the model.

		Args:
		    self: Refer to the current object

		Returns:
		    The embedding layer of the model
		"""
		raise NotImplementedError()

	def set_input_embeddings(self, value):
		"""The set_input_embeddings function is used to set the embedding module of the model.

		Args:
		    self: Represent the instance of the class
		    value: Set the embeddings of the model
		"""
		raise NotImplementedError()

	def get_output_embeddings(self):
		"""The get_output_embeddings function returns the output embeddings of a model.

		Args:
		    self: Represent the instance of the class

		Returns:
		    The output embeddings of the model
		"""
		raise NotImplementedError()

	def set_output_embeddings(self, new_embeddings):
		"""The set_output_embeddings function is used to set the output embeddings of a model.
		This function can be used to change the output embedding layer of a pretrained model in order to finetune it
		to some downstream task. Changing this layer has an effect only if the model has already been fine-tuned on some
		task (e.g., for classification). If you are training your own language models, you should call this function before
		you start training.

		Args:
		    self: Represent the instance of the class
		    new_embeddings: Set the embeddings of the output layer

		Returns:
		    A new embedding layer
		"""
		raise NotImplementedError()

	def set_decoder(self, decoder):
		"""The set_decoder function is used to set the decoder for a given encoder.

		Args:
		    self: Refer to the object itself
		    decoder: Set the decoder for a given encoder

		Returns:
		    A decoder
		"""
		raise NotImplementedError()

	def get_decoder(self):
		"""The get_decoder function is used to create a decoder object.

		Args:
		    self: Represent the instance of the class

		Returns:
		    A decoder object
		"""
		raise NotImplementedError()

	def init_cache(self, batch_size: int, max_length: int):
		def init_fn():
			input_ids = jnp.ones((batch_size, max_length), dtype=jnp.int32)
			attention_mask = jnp.ones_like(input_ids)
			position_ids = jnp.broadcast_to(
				jnp.arange(jnp.atleast_2d(input_ids).shape[-1]),
				input_ids.shape,
			)
			init_variables = self.module.init(
				jax.random.PRNGKey(0),
				input_ids,
				attention_mask,
				position_ids,
				return_dict=False,
				init_cache=True,
			)
			return init_variables["cache"]

		return jax.tree_map(lambda x: jnp.zeros(x.shape, x.dtype), jax.eval_shape(init_fn))

	def prepare_inputs_for_generation(
		self,
		input_ids,
		max_length,
		attention_mask: Optional[chex.Array] = None,
	):
		"""The prepare_inputs_for_generation function is used to prepare the inputs for a generation task.

		Args:
		    self: Access variables that belong to the class
		    input_ids: Pass in the input tokens
		    max_length: Set the length of the sequence to be generated
		    attention_mask: Optional[chex.Array]: Mask the attention
		        weights

		Returns:
		    A dictionary of the past_key_values, attention_mask and
		    position ids
		"""
		batch_size, seq_length = input_ids.shape

		past_key_values = self.init_cache(batch_size, max_length)
		extended_attention_mask = jnp.ones((batch_size, max_length), dtype="i4")
		if attention_mask is not None:
			position_ids = attention_mask.cumsum(axis=-1) - 1
			extended_attention_mask = jax.lax.dynamic_update_slice(
				extended_attention_mask, attention_mask, (0, 0)
			)
		else:
			position_ids = jnp.broadcast_to(
				jnp.arange(seq_length, dtype="i4")[None, :], (batch_size, seq_length)
			)

		return {
			"past_key_values": past_key_values,
			"attention_mask": extended_attention_mask,
			"position_ids": position_ids,
		}

	def update_inputs_for_generation(self, model_outputs, model_kwargs):
		model_kwargs["past_key_values"] = model_outputs.past_key_values
		model_kwargs["position_ids"] = model_kwargs["position_ids"][:, -1:] + 1
		return model_kwargs

	def __call__(
		self,
		input_ids: chex.Array,
		attention_mask: Optional[chex.Array] = None,
		position_ids: Optional[chex.Array] = None,
		params: dict = None,
		past_key_values: Optional[dict] = None,
		dropout_rng: jax.random.PRNGKey = None,
		train: bool = False,
		output_attentions: Optional[bool] = None,
		output_hidden_states: Optional[bool] = None,
		return_dict: Optional[bool] = None,
		extra_embedding: Optional[Union[jnp.ndarray, None]] = None,
		add_params_field: bool = False,
		vision_mask: Optional[chex.Array] = None,
		**kwargs,
	):
		raise NotImplementedError("Not Implemented Yet")

	def __repr__(self):
		"""The __repr__ function is used to generate a string representation of an object.
		This function should return a string that can be parsed by the Python interpreter
		to recreate the object. The __repr__ function is called when you use print() on an
		object, or when you type its name in the REPL.

		Args:
		    self: Refer to the instance of the class

		Returns:
		    A string representation of the object
		"""
		string = f"{self.__class__.__name__}(\n"
		for k, v in self.__dict__.items():
			if not k.startswith("_"):
				try:
					repr_src = f"  {k} : " + v.__str__().replace("\n", "\n  ") + "\n"
					string += (
						repr_src
						if len(repr_src) < 500
						else f"  {k} : " + f"{v.__class__.__name__}(...)" + "\n"
					)
				except TypeError:
					pass
		return string.strip() + "\n)"

	def __str__(self):
		"""The __str__ function is called when you use the print function or when str() is used.
		It should return a string representation of the object.

		Args:
		    self: Refer to the instance of the class

		Returns:
		    The object's string representation
		"""
		return self.__repr__()

	@property
	def config(self) -> EDPretrainedConfig:
		return self._config  # type:ignore

	def to_easydel_state(
		self,
		params: flax.core.FrozenDict,
		auto_check_params: bool = True,
	):
		"""
		Convert the Model to EasyDeLState
		"""
		if auto_check_params:
			gp = params.get("params", None)
			params = flax.core.FrozenDict(
				{"params": params} if gp is None else {"params": gp}
			)
		return EasyDeLState.load(
			apply_fn=self.__call__,
			params=params,
			opt_state=None,
			module_config=self.config,
			module=self,
			step=0,
		)

	def to_pytorch(
		self,
		params: FrozenDict,
		base_hf_auto_class=None,
		easystate_to_huggingface_model_kwargs: Optional[dict] = None,
	):
		"""
		Return the Huggingface / Pytorch implementation of the model with same weights  (if model is available in HF)
		"""
		if base_hf_auto_class is None:
			from transformers import AutoModelForCausalLM as base_hf_auto_class
		from easydel.transform.parameters_transformation import (
			easystate_to_huggingface_model,
		)

		state = self.to_easydel_state(params=params)
		if easystate_to_huggingface_model_kwargs is None:
			easystate_to_huggingface_model_kwargs = {}

		model_config = state.module_config
		if model_config is None:
			model_config = state.module.config_class
		# model_type = model_config.model_type
		model_class = base_hf_auto_class._model_mapping[type(model_config)]  # noqa
		hf_model = easystate_to_huggingface_model(
			state=state,
			base_huggingface_module=model_class,
			config=model_config,
			**easystate_to_huggingface_model_kwargs,
		)
		return hf_model

	@staticmethod
	def to_8bit(params, quantization_fields=None):
		if quantization_fields is None:
			quantization_fields = ["kernel", "embedding"]

		def quantize_params(params: dict) -> dict:
			"""Quantizes model parameters using Array8Bit.

			Args:
			    params: A dictionary of model parameters.

			Returns:
			    A dictionary of quantized model parameters.
			"""

			def q(path: str, array: Any) -> Array8Bit:
				"""Quantizes a single parameter array."""
				path = [p for p in path[0].key]
				for field in quantization_fields:
					if field in path:
						return Array8Bit.quantize(array, qk=64)
				return array

			return unflatten_dict(
				jax.tree_util.tree_map_with_path(
					q,
					flatten_dict(params),
				)
			)

		return quantize_params(params)

	def _model_card(self, name, repo_id):
		from easydel.utils.readme_generator import ReadmeGenerator, ModelInfo
		from easydel import __version__

		return ReadmeGenerator().generate_readme(
			ModelInfo(
				name=name,
				type=self.__class__.__name__,
				repo_id=repo_id,
				model_class=self.config_class.model_type,
				version=__version__,
			)
		)

	def save_pretrained(  # noqa
		self,
		save_directory: Union[str, os.PathLike],
		params,
		push_to_hub=False,
		token: Optional[Union[str, bool]] = None,
		gather_fns: dict[Callable] = None,
		float_dtype=None,
		verbose: bool = True,
		mismatch_allowed: bool = True,
		safe=True,
		**kwargs,
	):
		if token is not None:
			kwargs["token"] = token
		if os.path.isfile(save_directory):
			logger.error(
				f"Provided path ({save_directory}) should be a directory, not a file"
			)
			return
		os.makedirs(save_directory, exist_ok=True)
		repo_id = kwargs.pop("repo_id", save_directory.split(os.path.sep)[-1])
		if push_to_hub:
			commit_message = kwargs.pop("commit_message", None)
			repo_id = self._create_repo(repo_id, **kwargs)
			files_timestamps = self._get_files_timestamps(save_directory)
		save_directory = os.path.abspath(save_directory)
		self.config.architectures = [self.__class__.__name__[4:]]
		config = deepcopy(self.config)
		config.__dict__.pop("attn_dtype", None)  # make sure dtypes are not included
		config.save_pretrained(save_directory)
		if self.can_generate():
			self.generation_config.save_pretrained(save_directory)
		output_model_file = os.path.join(save_directory, "easydel-model.parameters")
		readme_path = os.path.join(save_directory, "README.md")
		if not os.path.exists(readme_path):
			open(readme_path, "w").write(self._model_card(repo_id, repo_id))
		func = (
			CheckpointManager.save_checkpoint_safe
			if (safe)
			else CheckpointManager.save_state_to_file
		)
		func(
			path=output_model_file,
			gather_fns=gather_fns,
			mismatch_allowed=mismatch_allowed,
			state=params,
			float_dtype=float_dtype,
			verbose=verbose,
		)

		logger.info(f"Model weights saved in {output_model_file}")

		if push_to_hub:
			self._upload_modified_files(
				save_directory,
				repo_id,
				files_timestamps,
				commit_message=commit_message,
				token=token,
			)

	def push_to_hub(
		self,
		repo_id: str,
		params,
		use_temp_dir: Optional[bool] = None,
		commit_message: Optional[str] = None,
		private: Optional[bool] = None,
		token: Optional[Union[bool, str]] = None,
		create_pr: bool = False,
		safe_serialization: bool = True,
		gather_fns: dict[Callable] = None,
		float_dtype=None,
		verbose: bool = True,
		mismatch_allowed: bool = True,
		revision: str = None,
		commit_description: str = None,
		tags: Optional[List[str]] = None,
	) -> str:
		working_dir = repo_id.split("/")[-1]

		repo_id = self._create_repo(
			repo_id,
			private=private,
			token=token,
			repo_url=None,
			organization=None,
		)

		if use_temp_dir is None:
			use_temp_dir = not os.path.isdir(working_dir)

		with working_or_temp_dir(
			working_dir=working_dir, use_temp_dir=use_temp_dir
		) as work_dir:
			files_timestamps = self._get_files_timestamps(work_dir)

			# Save all files.
			self.save_pretrained(
				work_dir,
				params=params,
				mismatch_allowed=mismatch_allowed,
				safe=safe_serialization,
				gather_fns=gather_fns,
				float_dtype=float_dtype,
				verbose=verbose,
				repo_id=repo_id,
			)

			return self._upload_modified_files(
				work_dir,
				repo_id,
				files_timestamps,
				commit_message=commit_message,
				token=token,
				create_pr=create_pr,
				revision=revision,
				commit_description=commit_description,
			)

	@classmethod
	def can_generate(cls) -> bool:
		"""
		Returns whether this model can generate sequences with `.generate()`. Returns:
		    `bool`: Whether this model can generate sequences with `.generate()`.
		"""
		# Detects whether `prepare_inputs_for_generation` has been overwritten, which is a requirement for generation.
		# Alternativelly, the model can also have a custom `generate` function.
		if "GenerationMixin" in str(
			cls.prepare_inputs_for_generation
		) and "GenerationMixin" in str(cls.generate):
			return False
		return True

	@classmethod
	def from_pretrained(
		cls,
		pretrained_model_name_or_path: Union[str, os.PathLike],
		sharding_axis_dims: Sequence[int] = (1, -1, 1, 1),
		sharding_axis_names: Sequence[str] = ("dp", "fsdp", "tp", "sp"),
		partition_axis: PartitionAxis = PartitionAxis(),  # noqa
		dtype: jnp.dtype = jnp.float32,
		param_dtype: jnp.dtype = jnp.float32,
		safe: bool = True,
		precision: jax.lax.PrecisionLike = jax.lax.Precision("fastest"),  # noqa
		input_shape: Optional[Tuple[int, int]] = None,
		config_kwargs: Optional[dict[str, Any]] = None,
		partition_rules: Optional[Tuple[Tuple[str, PartitionSpec]]] = None,
		quantization_method: Optional[Literal["nf4", "8bit"]] = None,
		quantization_platform: Optional[Literal["jax", "triton", "pallas"]] = "jax",
		backend: Optional[Literal["cpu", "gpu", "tpu"]] = None,
		platform: Optional[Literal["jax", "triton", "pallas"]] = "jax",
		bit_targeted_params: Optional[List[str]] = None,
		quantization_block_size: int = 4096,
		shard_fns: dict[Callable] = None,
		auto_shard_params: bool = False,
		remove_dict_prefix=None,
		verbose: bool = True,
		mismatch_allowed: bool = True,
		*model_args,
		config: Optional[Union[EDPretrainedConfig, str, os.PathLike]] = None,
		cache_dir: Optional[Union[str, os.PathLike]] = None,
		ignore_mismatched_sizes: bool = False,
		force_download: bool = False,
		local_files_only: bool = False,
		token: Optional[Union[str, bool]] = None,
		revision: str = "main",
		**kwargs,
	):
		"""
		loads EasyDeL Models
		"""

		from transformers import GenerationConfig
		from transformers.utils import cached_file as _cached_file
		from transformers.utils import download_url as _download_url
		from transformers.utils import is_offline_mode as _is_offline_mode
		from transformers.utils import is_remote_url as _is_remote_url

		proxies = kwargs.pop("proxies", None)
		trust_remote_code = kwargs.pop("trust_remote_code", None)
		from_pipeline = kwargs.pop("_from_pipeline", None)
		from_auto_class = kwargs.pop("_from_auto", False)
		subfolder = kwargs.pop("subfolder", "")
		commit_hash = kwargs.pop("_commit_hash", None)

		# Not relevant for Flax Models
		_ = kwargs.pop("adapter_kwargs", None)

		if trust_remote_code is True:
			logger.warning(
				"The argument `trust_remote_code` is to be used with Auto classes. It has no effect here and is"
				" ignored."
			)

		user_agent = {
			"file_type": "model",
			"framework": "flax",
			"from_auto_class": from_auto_class,
		}
		if from_pipeline is not None:
			user_agent["using_pipeline"] = from_pipeline

		if _is_offline_mode() and not local_files_only:
			logger.info("Offline mode: forcing local_files_only=True")
			local_files_only = True

		if input_shape is None:
			cl_di = len(jax.devices())
			input_shape = (cl_di, cl_di)  # safest way to perform loading ...
		config_path = config if config is not None else pretrained_model_name_or_path
		from easydel.modules.auto_models import (
			AutoEasyDeLConfig,
			AutoShardAndGatherFunctions,
			get_modules_by_type,
		)

		config = AutoEasyDeLConfig.from_pretrained(
			config_path,
			sharding_axis_dims=sharding_axis_dims,
			sharding_axis_names=sharding_axis_names,
			partition_axis=partition_axis,
			from_torch=False,
			backend=backend,
			platform=platform,
		)

		if config_kwargs is not None:
			for k, v in config_kwargs.items():
				setattr(config, k, v)
		_, model_kwargs = EDPretrainedConfig.from_pretrained(
			config_path,
			cache_dir=cache_dir,
			return_unused_kwargs=True,
			force_download=force_download,
			proxies=proxies,
			local_files_only=local_files_only,
			token=token,
			revision=revision,
			subfolder=subfolder,
			_from_auto=from_auto_class,
			_from_pipeline=from_pipeline,
			_commit_hash=commit_hash,
			**kwargs,
		)
		if commit_hash is None:
			commit_hash = getattr(config, "_commit_hash", None)
		if auto_shard_params and shard_fns is None:
			shard_fns, _ = AutoShardAndGatherFunctions.from_config(
				config=config,
				input_shape=input_shape,
				flatten=False,
				partition_rules=partition_rules,
			)
			fns = {"params": shard_fns}
			fns.update(shard_fns)
			shard_fns = fns
		elif auto_shard_params and shard_fns is not None:
			logger.warning(
				"`auto_shard_params` will be ignored since `shard_fns` is provided."
			)
		if pretrained_model_name_or_path is not None:
			pretrained_model_name_or_path = str(pretrained_model_name_or_path)
			is_local = os.path.isdir(pretrained_model_name_or_path)
			if os.path.isdir(pretrained_model_name_or_path):
				if os.path.isfile(
					os.path.join(pretrained_model_name_or_path, subfolder, FLAX_WEIGHTS_NAME)
				):
					archive_file = os.path.join(
						pretrained_model_name_or_path, subfolder, FLAX_WEIGHTS_NAME
					)
				else:
					raise EnvironmentError(
						f"Error no file named {FLAX_WEIGHTS_NAME} found in directory {pretrained_model_name_or_path}"
					)
			elif os.path.isfile(os.path.join(subfolder, pretrained_model_name_or_path)):
				archive_file = pretrained_model_name_or_path
				is_local = True
			elif _is_remote_url(pretrained_model_name_or_path):
				filename = pretrained_model_name_or_path
				resolved_archive_file = _download_url(pretrained_model_name_or_path)
			else:
				filename = FLAX_WEIGHTS_NAME
				try:
					cached_file_kwargs = {
						"cache_dir": cache_dir,
						"force_download": force_download,
						"proxies": proxies,
						"local_files_only": local_files_only,
						"token": token,
						"user_agent": user_agent,
						"revision": revision,
						"subfolder": subfolder,
						"_raise_exceptions_for_gated_repo": False,
						"_raise_exceptions_for_missing_entries": False,
						"_commit_hash": commit_hash,
					}
					resolved_archive_file = _cached_file(
						pretrained_model_name_or_path,
						filename,
						**cached_file_kwargs,
					)

					if resolved_archive_file is None:
						raise EnvironmentError("no model parameters found!")
				except EnvironmentError:
					raise
				except Exception:
					raise EnvironmentError(
						f"Can't load the model for '{pretrained_model_name_or_path}'. If you were trying to load it"
						" from 'https://huggingface.co/models', make sure you don't have a local directory with the"
						f" same name. Otherwise, make sure '{pretrained_model_name_or_path}' is the correct path to a"
						f" directory containing a file named {FLAX_WEIGHTS_NAME}."
					) from None

			if is_local:
				logger.info(f"loading weights file {archive_file}")
				resolved_archive_file = archive_file
				filename = resolved_archive_file.split(os.path.sep)[-1]
			else:
				logger.info(
					f"loading weights file {filename} from cache at {resolved_archive_file}"
				)
		else:
			resolved_archive_file = None

		if cls.__name__ == "EDPretrainedModel":
			# if they are using EDPretrainedModel.from_pretrained
			# they will get error AssertionError: `module` must be provided.` so we autoset this to make sure user don't
			# experience this error.
			_, cls, _ = get_modules_by_type(config.model_type)
		model = cls(
			config=config,
			dtype=dtype,
			param_dtype=param_dtype,
			precision=precision,
			_do_init=False,
		)
		if safe:
			state, _ = CheckpointManager.load_checkpoint_safe(
				path=resolved_archive_file,
				mismatch_allowed=mismatch_allowed,
				verbose=verbose,
				shard_fns=shard_fns,
			)
		else:
			state = CheckpointManager.load_checkpoint(
				path=resolved_archive_file,
				mismatch_allowed=mismatch_allowed,
				verbose=verbose,
				shard_fns=shard_fns,
				remove_dict_prefix=remove_dict_prefix,
			)

		params = state.get("params", None)
		if params is not None:
			state = params

		state = flatten_dict(state)
		random_state = flatten_dict(unfreeze(model.params_shape_tree))

		missing_keys = model.required_params - set(state.keys())
		unexpected_keys = set(state.keys()) - model.required_params

		# Disabling warning when porting pytorch weights to flax, flax does not uses num_batches_tracked
		for unexpected_key in unexpected_keys.copy():
			if "num_batches_tracked" in unexpected_key[-1]:
				unexpected_keys.remove(unexpected_key)

		if missing_keys:
			logger.warning(
				f"The checkpoint {pretrained_model_name_or_path} is missing required keys: {missing_keys}. "
				"Make sure to call model.init_weights to initialize the missing weights."
			)
			cls._missing_keys = missing_keys

		mismatched_keys = []
		for key in state.keys():
			if key in random_state and state[key].shape != random_state[key].shape:
				if ignore_mismatched_sizes:
					mismatched_keys.append((key, state[key].shape, random_state[key].shape))
					state[key] = random_state[key]
				else:
					raise ValueError(
						f"Trying to load the pretrained weight for {key} failed: checkpoint has shape "
						f"{state[key].shape} which is incompatible with the model shape {random_state[key].shape}. "
						"Using `ignore_mismatched_sizes=True` if you really want to load this checkpoint inside this "
						"model."
					)

		if missing_keys:
			for missing_key in missing_keys:
				state[missing_key] = random_state[missing_key]

		# remove unexpected keys to not be saved again
		for unexpected_key in unexpected_keys:
			del state[unexpected_key]

		if len(unexpected_keys) > 0:
			logger.warning(
				f"Some weights of the model checkpoint at {pretrained_model_name_or_path} were not used when"
				f" initializing {model.__class__.__name__}: {unexpected_keys}\n- This IS expected if you are"
				f" initializing {model.__class__.__name__} from the checkpoint of a model trained on another task or"
				" with another architecture (e.g. initializing a BertForSequenceClassification model from a"
				" BertForPreTraining model).\n- This IS NOT expected if you are initializing"
				f" {model.__class__.__name__} from the checkpoint of a model that you expect to be exactly identical"
				" (initializing a BertForSequenceClassification model from a BertForSequenceClassification model)."
			)

		if len(missing_keys) > 0:
			logger.warning(
				f"Some weights of {model.__class__.__name__} were not initialized from the model checkpoint at"
				f" {pretrained_model_name_or_path} and are newly initialized: {missing_keys}\nYou should probably"
				" TRAIN this model on a down-stream task to be able to use it for predictions and inference."
			)
		if len(mismatched_keys) > 0:
			mismatched_warning = "\n".join(
				[
					f"- {key}: found shape {shape1} in the checkpoint and {shape2} in the model instantiated"
					for key, shape1, shape2 in mismatched_keys
				]
			)
			logger.warning(
				f"Some weights of {model.__class__.__name__} were not initialized from the model checkpoint at"
				f" {pretrained_model_name_or_path} and are newly initialized because the shapes did not"
				f" match:\n{mismatched_warning}\nYou should probably TRAIN this model on a down-stream task to be able"
				" to use it for predictions and inference."
			)

		if model.can_generate():
			try:
				model.generation_config = GenerationConfig.from_pretrained(
					pretrained_model_name_or_path,
					cache_dir=cache_dir,
					force_download=force_download,
					proxies=proxies,
					local_files_only=local_files_only,
					token=token,
					revision=revision,
					subfolder=subfolder,
					_from_auto=from_auto_class,
					_from_pipeline=from_pipeline,
					**kwargs,
				)
			except OSError:
				logger.info(
					"Generation config file not found, using a generation config created from the model config."
				)
				pass

		if bit_targeted_params is None:
			params_pattern_selection = re.compile(DEFAULT_QUANTIZATION_PATTERN)
		else:
			params_pattern_selection = bit_targeted_params
		if quantization_method is not None:
			quantizer = EasyQuantizer(
				quantization_method=quantization_method,
				block_size=quantization_block_size,
				quantization_platform=quantization_platform,
			)
			pbar = tqdm(list(state.keys()))
			for key_tuple in pbar:
				if (
					quantizer is not None
					and key_tuple[-1] != "embedding"
					and params_pattern_selection.search("/".join(key_tuple))
				):
					state[key_tuple] = quantizer(array=state[key_tuple])
					pbar.set_description(
						f"Quantizing {state[key_tuple].shape} "
						f"({quantization_method} | {quantization_platform})"
					)
		return model, unflatten_dict(state)

	def shard_params(
		self,
		params,
		partition_rules: Optional[
			Union[Mapping[str, Callable], Mapping[tuple, Callable]]
		] = None,
		mesh: Optional[jax.sharding.Mesh] = None,
	):
		"""
		Shards model parameters according to the provided partition rules.

		Args:
		    params: A PyTree representing the model parameters.
		    partition_rules: A dictionary mapping parameter names or a tuple of parameter names to
		        partitioning functions. The partitioning functions should take the shape and dtype of
		        the parameter as input and return a `jax.sharding.PartitionSpec`. If `None`, defaults to
		        the partition rules specified in the model configuration for fully sharded data parallelism.
		    mesh: The `jax.sharding.Mesh` object specifying the device mesh. If `None`, defaults to the mesh
		        defined in the model configuration.

		Returns:
		    A sharded version of the input parameters, where each parameter is partitioned across devices
		    according to the specified rules and mesh.
		"""
		if mesh is None:
			mesh = self.config.mesh
		if partition_rules is None:
			partition_rules = self.config.get_partition_rules(
				fully_sharded_data_parallel=True
			)
		partition_specs = fjformer.sharding.match_partition_rules(
			rules=partition_rules,
			params=params,
		)
		shard_fns = fjformer.sharding.make_shard_and_gather_fns(
			partition_specs=partition_specs,
			mesh=mesh,
		)[0]
		params = jax.tree_util.tree_map(
			lambda f, x: f(x),
			shard_fns,
			params,
		)
		return params

	def gather_params(
		self,
		params,
		partition_rules: Optional[
			Union[Mapping[str, Callable], Mapping[tuple, Callable]]
		] = None,
		mesh: Optional[jax.sharding.Mesh] = None,
	):
		"""
		Gathers sharded model parameters to the host device.

		This method reverses the sharding process performed by `shard_params`, collecting the parameter shards
		from different devices and aggregating them into a single PyTree on the host device.

		Args:
		    params: A PyTree representing the sharded model parameters.
		    partition_rules: A dictionary mapping parameter names or a tuple of parameter names to
		        partitioning functions. The partitioning functions should take the shape and dtype of
		        the parameter as input and return a `jax.sharding.PartitionSpec`. If `None`, defaults to
		        the partition rules specified in the model configuration for fully sharded data parallelism.
		    mesh: The `jax.sharding.Mesh` object specifying the device mesh. If `None`, defaults to the mesh
		        defined in the model configuration.

		Returns:
		    A non-sharded version of the input parameters, where all parameters are gathered onto the host device.
		"""
		if mesh is None:
			mesh = self.config.mesh
		if partition_rules is None:
			partition_rules = self.config.get_partition_rules(
				fully_sharded_data_parallel=True
			)
		partition_specs = fjformer.sharding.match_partition_rules(
			rules=partition_rules,
			params=params,
		)
		gather_fns = fjformer.sharding.make_shard_and_gather_fns(
			partition_specs=partition_specs,
			mesh=mesh,
		)[1]
		params = jax.tree_util.tree_map(
			lambda f, x: f(x),
			gather_fns,
			params,
		)
		return params
