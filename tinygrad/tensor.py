# inspired by https://github.com/karpathy/micrograd/blob/master/micrograd/engine.py
import inspect
import functools
import os
from collections import defaultdict
import numpy as np

# **** profiler ****

DEBUG = os.getenv("DEBUG", None) is not None
if DEBUG:
  import atexit, time
  debug_counts, debug_times = defaultdict(int), defaultdict(float)
  def print_debug_exit():
    for name, _ in sorted(debug_times.items(), key=lambda x: -x[1]):
      print(f"{name:>20} : {debug_counts[name]:>6} {debug_times[name]:>10.2f} ms")
  atexit.register(print_debug_exit)

class ProfileOp:
  def __init__(self, name, x, backward=False):
    self.name, self.x, self.output = f"back_{name}" if backward else name, x, None
  def __enter__(self):
    if DEBUG: self.st = time.time()
    return self
  def __exit__(self, *junk):
    if DEBUG:
      # TODO: fix this
      #if cl_queue is not None:
      #  cl_queue.finish()
      et = (time.time()-self.st)*1000.
      debug_counts[self.name] += 1
      debug_times[self.name] += et
      print(f"{self.name:>20} : {et:>7.2f} ms {str([y.shape for y in self.x]):>40} {'-> '+str(self.output.shape) if self.output is not None else ''}")

# **** enumerate supported devices ****

class Device:
  _ops = sorted(os.listdir(os.path.join(os.path.dirname(os.path.realpath(__file__)), "ops")))
  imports = dict(enumerate([os.path.splitext(x)[0] for x in _ops if x.startswith("ops_")]))
  DEFAULT = None
  buffers = {}
  for i,op in imports.items():
    name = op[len("ops_"):].upper()
    vars()[name] = i
    DEFAULT = i if os.environ.get(name, 0) == "1" else DEFAULT
  DEFAULT = CPU if DEFAULT is None else DEFAULT

# **** start with two base classes, Tensor and Function ****

class Tensor:
  did_float_warning = False
  training = True
  ops = defaultdict(dict)

  def __init__(self, data, device=Device.DEFAULT, requires_grad=True):
    self.device, self.data = device, self._move_data(data, device)

    self.grad, self.requires_grad = None, requires_grad

    # internal variables used for autograd graph construction
    self._ctx = None

  def __repr__(self):
    return f"<Tensor {self.data!r} with grad {(self.grad.data if self.grad else None)!r}>"

  def assign(self, x):
    if not isinstance(x, Tensor):
      x = Tensor(x)
    assert self.shape == x.shape
    self.data = x.data

  @property
  def shape(self):
    return self.data.shape

  @staticmethod
  def _get_data_dtype(data):
    return data.getdtype() if getattr(data, 'getdtype', None) else data.dtype

  @property
  def dtype(self):
    return Tensor._get_data_dtype(self.data)

  # ***** creation helper functions *****

  @classmethod
  def zeros(cls, *shape, **kwargs):
    return cls(np.zeros(shape, dtype=np.float32), **kwargs)

  @classmethod
  def ones(cls, *shape, **kwargs):
    return cls(np.ones(shape, dtype=np.float32), **kwargs)

  @classmethod
  def randn(cls, *shape, **kwargs):
    return cls(np.random.randn(*shape).astype(np.float32), **kwargs)
  
  @classmethod
  def arange(cls, stop, start=0, **kwargs):
    return cls(np.arange(start=start, stop=stop).astype(np.float32), **kwargs)

  @classmethod
  def uniform(cls, *shape, **kwargs):
    return cls((np.random.uniform(-1., 1., size=shape)/np.sqrt(np.prod(shape))).astype(np.float32), **kwargs)

  @classmethod
  def eye(cls, dim, **kwargs):
    return cls(np.eye(dim).astype(np.float32), **kwargs)

  # ***** toposort and backward pass *****

  def deepwalk(self):
    def _deepwalk(node, visited, nodes):
      visited.add(node)
      if node._ctx:
        [_deepwalk(i, visited, nodes) for i in node._ctx.parents if i not in visited]
        nodes.append(node)
      return nodes
    return _deepwalk(self, set(), [])

  def backward(self):
    assert self.shape == (1,)

    # fill in the first grad with one
    # this is "implicit gradient creation"
    self.grad = Tensor(np.ones(self.shape, dtype=self.dtype), device=self.device, requires_grad=False)

    for t0 in reversed(self.deepwalk()):
      assert (t0.grad is not None)
      with ProfileOp(t0._ctx.__class__.__name__, [t0.grad], backward=True) as po:
        grads = t0._ctx.backward(t0._ctx, t0.grad.data)
      if len(t0._ctx.parents) == 1:
        grads = [grads]
      for t, g in zip(t0._ctx.parents, grads):
        if g is not None:
          assert g.shape == t.shape, \
            f"grad shape must match tensor shape in {self._ctx!r}, {g.shape!r} != {t.shape!r}"
          gt = Tensor(g, device=self.device, requires_grad=False)
          t.grad = gt if t.grad is None else (t.grad + gt)

  # ***** tinygrad supports many devices *****

  @staticmethod
  def _move_data(data, device):
    if isinstance(data, list):
      data = np.array(data, dtype=np.float32)
    if isinstance(data, np.ndarray):
      data = data.view(Device.buffers[Device.CPU])
    if isinstance(data, Device.buffers[device]):
      return data

    if Tensor._get_data_dtype(data) != np.float32 and not Tensor.did_float_warning:
      # warning? float64 is actually needed for numerical jacobian
      print(f"warning, {data.shape!r} isn't float32, it's {data.dtype}")
      Tensor.did_float_warning = True

    data = data.toCPU().view(Device.buffers[Device.CPU])
    return Device.buffers[device].fromCPU(data)

  def to_(self, device):
    self.data, self.device = self._move_data(self.data, device), device
    if self.grad: self.grad.to_(device)

  def to(self, device):
    ret = Tensor(self.data, device)
    if self.grad: ret.grad = self.grad.to(device)
    return ret

  def detach(self):
    return Tensor(self.data, device=self.device)

  # ***** non first class ops *****
  
  def __getitem__(self, val):
    arg = []
    new_shape = []
    if val is not None:
      for i, s in enumerate(val if isinstance(val, (list, tuple)) else [val]):
        if isinstance(s, int):
          arg.append((s, s + 1))
        else:
          arg.append((s.start if s.start is not None else 0,
            (s.stop if s.stop >=0 else self.shape[i]+s.stop) if s.stop is not None else self.shape[i]))
          new_shape.append(arg[-1][1] - arg[-1][0])
          assert s.step is None or s.step == 1
    new_shape += self.shape[len(arg):]
    return self.slice(arg = arg + [(0,self.shape[i]) for i in range(len(arg), len(self.shape))]).reshape(shape=new_shape)

  def cat(self, y, dim=0):
    assert len(self.shape) == len(y.shape)
    dim = (dim + len(self.shape)) if dim < 0 else dim
    s1, s2 = [], []
    for i in range(len(self.shape)):
      if i != dim:
        assert self.shape[i] == y.shape[i]
        s1.append((0, self.shape[i]))
        s2.append((0, self.shape[i]))
      else:
        s1.append((0, self.shape[i]+y.shape[i]))
        s2.append((-self.shape[i], y.shape[i]))
    return self.slice(arg=s1) + y.slice(arg=s2)

  def pad2d(self, padding):
    return self[:, :, -padding[2]:self.shape[2]+padding[3], -padding[0]:self.shape[3]+padding[1]]

  def dot(self, w):
    return self.matmul(w)

  def mean(self, axis=None):
    out = self.sum(axis=axis)
    return out * (np.prod(out.shape)/np.prod(self.shape))

  def sqrt(self):
    return self.pow(0.5)

  def div(self, y):
    return self * (y ** -1.0)
  __truediv__ = div

  def sigmoid(self):
    #e = self.exp(); return e.div(1 + e)
    return (1.0 + (0.0-self).exp()) ** -1.0

  def swish(self):
    return self * self.sigmoid()

  def relu6(self):
    return self.relu() - (self-6).relu()

  def hardswish(self):
    return self * (self+3).relu6() * (1/6)

  def tanh(self):
    return 2.0 * ((2.0 * self).sigmoid()) - 1.0

  def gelu(x):
    # https://github.com/huggingface/transformers/blob/master/src/transformers/activations.py
    #import torch; return Tensor(torch.nn.functional.gelu(torch.tensor(x.data)).numpy())
    return 0.5 * x * (1 + (x * 0.7978845608 * (1 + 0.044715 * x * x)).tanh())

  def leakyrelu(self, neg_slope=0.01):
    return self.relu() - (-neg_slope*self).relu()

  def softmax(self):
    ns = list(self.shape)[:-1]+[1]
    m = self.max(axis=len(self.shape)-1).reshape(shape=ns)
    e = (self - m).exp()
    ss = e.sum(axis=len(self.shape)-1).reshape(shape=ns)
    return e.div(ss)

  def logsoftmax(self):
    ns = list(self.shape)[:-1]+[1]
    m = self.max(axis=len(self.shape)-1).reshape(shape=ns)
    ss = m + (self-m).exp().sum(axis=len(self.shape)-1).reshape(shape=ns).log()
    return self - ss

  def dropout(self, p=0.5):
    if Tensor.training:
      _mask = np.asarray(np.random.binomial(1, 1.0-p, size=self.shape), dtype=self.dtype)
      return self * Tensor(_mask, requires_grad=False, device=self.device) * (1/(1.0 - p))
    else:
      return self

  def softplus(self, limit=20, beta=1):
    # safe softplus - 1/beta*log(1 + exp(beta*x)) (PyTorch)
    eb = (self*beta).exp()
    ret = (1 + eb).log()
    return (1/beta)*ret

  def mish(self):
    return self * (self.softplus().tanh()) # x*tanh(softplus(x))

  def abs(self):
    return self.relu() + (-1.0*self).relu()

  def sign(self):
    return self / (self.abs() + 1e-10)

  def _pool2d(self, py, px):
    xup = self[:, :, :self.shape[2]-self.shape[2]%py, :self.shape[3]-self.shape[3]%px]
    return xup.reshape(shape=(xup.shape[0], xup.shape[1], xup.shape[2]//py, py, xup.shape[3]//px, px))

  def avg_pool2d(self, kernel_size=(2,2)):
    return self._pool2d(*kernel_size).mean(axis=(3,5))

  def max_pool2d(self, kernel_size=(2,2)):
    return self._pool2d(*kernel_size).max(axis=(3,5))

  def affine(self, params):
    shp = [1] * (len(self.shape)-1) + [-1]
    if len(params[0].shape) == 1:   # elementwise affine
      return self.mul(params[0].reshape(shape=shp)).add(params[1].reshape(shape=shp))
    else:
      return self.dot(params[0]).add(params[1].reshape(shape=shp))

# An instantiation of the Function is the Context
class Function:
  def __new__(cls, *args, **kwargs):
    cls.forward = staticmethod(cls.forward)
    cls.backward = staticmethod(cls.backward)
    return super().__new__(cls)

  def __init__(self, *tensors):
    self.parents = tensors
    self.saved_tensors = []

  def save_for_backward(self, *x):
    self.saved_tensors.extend(x)

  def apply(self, *x, **kwargs):
    ctx = self(*x) # self - operation i.e 'add', 'sub', etc.
    # use default params
    params = inspect.signature(self.forward).parameters
    for p in params.values():
      if p.default is not p.empty:
        setattr(ctx, p.name, p.default)
    # overwrite with passed params
    for k, v in kwargs.items():
      setattr(ctx, k, v)
    with ProfileOp(ctx.__class__.__name__, x) as po:
      po.output = ret = Tensor(self.forward(ctx, *[t.data for t in x], **kwargs),
                   device=ctx.device, requires_grad=any([t.requires_grad for t in x]))
    if ret.requires_grad:
      ret._ctx = ctx
    return ret

def register(name, fxn, device=Device.CPU):
  Tensor.ops[device][name] = fxn
  def dispatch(*x, **kwargs):
    tt = [arg for arg in x if isinstance(arg, Tensor)][0]
    x = [Tensor(np.array([arg], dtype=tt.dtype), device=tt.device, requires_grad=False) if not isinstance(arg, Tensor) else arg for arg in x]
    f = Tensor.ops[tt.device][name]
    #f.cl_ctx, f.cl_queue, f.device = cl_ctx, cl_queue, tt.device
    f.device = tt.device
    return f.apply(f, *x, **kwargs)
  setattr(Tensor, name, dispatch)
  if name in ['add', 'sub', 'mul', 'pow', 'matmul']:
    setattr(Tensor, f"__{name}__", dispatch)
    setattr(Tensor, f"__i{name}__", lambda self,x: self.assign(dispatch(self,x)))
    setattr(Tensor, f"__r{name}__", lambda self,x: dispatch(x,self))

for device in [device for device in Device.__dict__.keys() if device[0] != "_"]:
  setattr(Tensor, f"{device.lower()}", functools.partialmethod(Tensor.to, Device.__dict__[device]))
  setattr(Tensor, f"{device.lower()}_", functools.partialmethod(Tensor.to_, Device.__dict__[device]))

# this registers all the operations
def _register_ops(namespace, device=Device.CPU):
  for name, cls in inspect.getmembers(namespace, inspect.isclass):
    if name.endswith("Buffer"):  Device.buffers[device] = cls
    elif name[0] != "_":  register(name.lower(), cls, device=device)

import importlib
for d,ops in Device.imports.items():
  try:
    _register_ops(importlib.import_module('tinygrad.ops.'+ops), d)
  except ImportError:
    pass
