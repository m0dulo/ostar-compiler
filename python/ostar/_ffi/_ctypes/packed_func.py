
# coding: utf-8
# pylint: disable=invalid-name, protected-access, too-many-branches, global-statement, unused-import
"""Function configuration API."""
import ctypes
import traceback
from numbers import Number, Integral

from ..base import _LIB, get_last_ffi_error, py2cerror, check_call
from ..base import c_str, string_types
from ..runtime_ctypes import DataType, OSTARByteArray, Device, ObjectRValueRef
from . import ndarray as _nd
from .ndarray import NDArrayBase, _make_array
from .types import OSTARValue, ArgTypeCode
from .types import OSTARPackedCFunc, OSTARCFuncFinalizer
from .types import RETURN_SWITCH, C_TO_PY_ARG_SWITCH, _wrap_arg_func, _device_to_int64
from .object import ObjectBase, PyNativeObject, _set_class_object
from . import object as _object

PackedFuncHandle = ctypes.c_void_p
ModuleHandle = ctypes.c_void_p
ObjectHandle = ctypes.c_void_p
OSTARRetValueHandle = ctypes.c_void_p


def _ctypes_free_resource(rhandle):
    """callback to free resources when it is not needed."""
    pyobj = ctypes.cast(rhandle, ctypes.py_object)
    ctypes.pythonapi.Py_DecRef(pyobj)


# Global callback that is always alive
OSTAR_FREE_PYOBJ = OSTARCFuncFinalizer(_ctypes_free_resource)
ctypes.pythonapi.Py_IncRef(ctypes.py_object(OSTAR_FREE_PYOBJ))


def _make_packed_func(handle, is_global):
    """Make a packed function class"""
    obj = _CLASS_PACKED_FUNC.__new__(_CLASS_PACKED_FUNC)
    obj.is_global = is_global
    obj.handle = handle
    return obj


def convert_to_ostar_func(pyfunc):
    """Convert a python function to OSTAR function

    Parameters
    ----------
    pyfunc : python function
        The python function to be converted.

    Returns
    -------
    ostarfunc: ostar.nd.Function
        The converted ostar function.
    """
    local_pyfunc = pyfunc

    def cfun(args, type_codes, num_args, ret, _):
        """ctypes function"""
        num_args = num_args.value if isinstance(num_args, ctypes.c_int) else num_args
        pyargs = (C_TO_PY_ARG_SWITCH[type_codes[i]](args[i]) for i in range(num_args))
        # pylint: disable=broad-except
        try:
            rv = local_pyfunc(*pyargs)
        except Exception:
            msg = traceback.format_exc()
            msg = py2cerror(msg)
            _LIB.OSTARAPISetLastError(c_str(msg))
            return -1

        if rv is not None:
            if isinstance(rv, tuple):
                raise ValueError("PackedFunction can only support one return value")
            temp_args = []
            values, tcodes, _ = _make_ostar_args((rv,), temp_args)
            if not isinstance(ret, OSTARRetValueHandle):
                ret = OSTARRetValueHandle(ret)
            if _LIB.OSTARCFuncSetReturn(ret, values, tcodes, ctypes.c_int(1)) != 0:
                raise get_last_ffi_error()
            _ = temp_args
            _ = rv
        return 0

    handle = PackedFuncHandle()
    f = OSTARPackedCFunc(cfun)
    # NOTE: We will need to use python-api to increase ref count of the f
    # OSTAR_FREE_PYOBJ will be called after it is no longer needed.
    pyobj = ctypes.py_object(f)
    ctypes.pythonapi.Py_IncRef(pyobj)
    if _LIB.OSTARFuncCreateFromCFunc(f, pyobj, OSTAR_FREE_PYOBJ, ctypes.byref(handle)) != 0:
        raise get_last_ffi_error()
    return _make_packed_func(handle, False)


def _make_ostar_args(args, temp_args):
    """Pack arguments into c args ostar call accept"""
    num_args = len(args)
    values = (OSTARValue * num_args)()
    type_codes = (ctypes.c_int * num_args)()
    for i, arg in enumerate(args):
        if isinstance(arg, ObjectBase):
            values[i].v_handle = arg.handle
            type_codes[i] = ArgTypeCode.OBJECT_HANDLE
        elif arg is None:
            values[i].v_handle = None
            type_codes[i] = ArgTypeCode.NULL
        elif isinstance(arg, NDArrayBase):
            values[i].v_handle = ctypes.cast(arg.handle, ctypes.c_void_p)
            type_codes[i] = (
                ArgTypeCode.NDARRAY_HANDLE if not arg.is_view else ArgTypeCode.DLTENSOR_HANDLE
            )
        elif isinstance(arg, PyNativeObject):
            values[i].v_handle = arg.__ostar_object__.handle
            type_codes[i] = ArgTypeCode.OBJECT_HANDLE
        elif isinstance(arg, _nd._OSTAR_COMPATS):
            values[i].v_handle = ctypes.c_void_p(arg._ostar_handle)
            type_codes[i] = arg.__class__._ostar_tcode
        elif isinstance(arg, Integral):
            values[i].v_int64 = arg
            type_codes[i] = ArgTypeCode.INT
        elif isinstance(arg, Number):
            values[i].v_float64 = arg
            type_codes[i] = ArgTypeCode.FLOAT
        elif isinstance(arg, DataType):
            values[i].v_str = c_str(str(arg))
            type_codes[i] = ArgTypeCode.STR
        elif isinstance(arg, Device):
            values[i].v_int64 = _device_to_int64(arg)
            type_codes[i] = ArgTypeCode.DLDEVICE
        elif isinstance(arg, (bytearray, bytes)):
            # from_buffer only taeks in bytearray.
            if isinstance(arg, bytes):
                byte_arr = bytearray(arg)
                temp_args.append(byte_arr)
                arg = byte_arr

            arr = OSTARByteArray()
            arr.data = ctypes.cast(
                (ctypes.c_byte * len(arg)).from_buffer(arg), ctypes.POINTER(ctypes.c_byte)
            )
            arr.size = len(arg)
            values[i].v_handle = ctypes.c_void_p(ctypes.addressof(arr))
            temp_args.append(arr)
            type_codes[i] = ArgTypeCode.BYTES
        elif isinstance(arg, string_types):
            values[i].v_str = c_str(arg)
            type_codes[i] = ArgTypeCode.STR
        elif isinstance(arg, (list, tuple, dict, _CLASS_OBJECT_GENERIC)):
            arg = _FUNC_CONVERT_TO_OBJECT(arg)
            values[i].v_handle = arg.handle
            type_codes[i] = ArgTypeCode.OBJECT_HANDLE
            temp_args.append(arg)
        elif isinstance(arg, _CLASS_MODULE):
            values[i].v_handle = arg.handle
            type_codes[i] = ArgTypeCode.MODULE_HANDLE
        elif isinstance(arg, PackedFuncBase):
            values[i].v_handle = arg.handle
            type_codes[i] = ArgTypeCode.PACKED_FUNC_HANDLE
        elif isinstance(arg, ctypes.c_void_p):
            values[i].v_handle = arg
            type_codes[i] = ArgTypeCode.HANDLE
        elif isinstance(arg, ObjectRValueRef):
            values[i].v_handle = ctypes.cast(ctypes.byref(arg.obj.handle), ctypes.c_void_p)
            type_codes[i] = ArgTypeCode.OBJECT_RVALUE_REF_ARG
        elif callable(arg):
            arg = convert_to_ostar_func(arg)
            values[i].v_handle = arg.handle
            type_codes[i] = ArgTypeCode.PACKED_FUNC_HANDLE
            temp_args.append(arg)
        else:
            raise TypeError("Don't know how to handle type %s" % type(arg))
    return values, type_codes, num_args


class PackedFuncBase(object):
    """Function base."""

    __slots__ = ["handle", "is_global"]
    # pylint: disable=no-member
    def __init__(self, handle, is_global):
        """Initialize the function with handle

        Parameters
        ----------
        handle : PackedFuncHandle
            the handle to the underlying function.

        is_global : bool
            Whether this is a global function in python
        """
        self.handle = handle
        self.is_global = is_global

    def __del__(self):
        if not self.is_global and _LIB is not None:
            if _LIB.OSTARFuncFree(self.handle) != 0:
                raise get_last_ffi_error()

    def __call__(self, *args):
        """Call the function with positional arguments

        args : list
           The positional arguments to the function call.
        """
        temp_args = []
        values, tcodes, num_args = _make_ostar_args(args, temp_args)
        ret_val = OSTARValue()
        ret_tcode = ctypes.c_int()
        if (
            _LIB.OSTARFuncCall(
                self.handle,
                values,
                tcodes,
                ctypes.c_int(num_args),
                ctypes.byref(ret_val),
                ctypes.byref(ret_tcode),
            )
            != 0
        ):
            raise get_last_ffi_error()
        _ = temp_args
        _ = args
        return RETURN_SWITCH[ret_tcode.value](ret_val)


def __init_handle_by_constructor__(fconstructor, args):
    """Initialize handle by constructor"""
    temp_args = []
    values, tcodes, num_args = _make_ostar_args(args, temp_args)
    ret_val = OSTARValue()
    ret_tcode = ctypes.c_int()
    if (
        _LIB.OSTARFuncCall(
            fconstructor.handle,
            values,
            tcodes,
            ctypes.c_int(num_args),
            ctypes.byref(ret_val),
            ctypes.byref(ret_tcode),
        )
        != 0
    ):
        raise get_last_ffi_error()
    _ = temp_args
    _ = args
    assert ret_tcode.value == ArgTypeCode.OBJECT_HANDLE
    handle = ret_val.v_handle
    return handle


def _return_module(x):
    """Return function"""
    handle = x.v_handle
    if not isinstance(handle, ModuleHandle):
        handle = ModuleHandle(handle)
    return _CLASS_MODULE(handle)


def _handle_return_func(x):
    """Return function"""
    handle = x.v_handle
    if not isinstance(handle, PackedFuncHandle):
        handle = PackedFuncHandle(handle)
    return _CLASS_PACKED_FUNC(handle, False)


def _get_global_func(name, allow_missing=False):
    handle = PackedFuncHandle()
    check_call(_LIB.OSTARFuncGetGlobal(c_str(name), ctypes.byref(handle)))

    if handle.value:
        return _make_packed_func(handle, False)

    if allow_missing:
        return None

    raise ValueError("Cannot find global function %s" % name)


# setup return handle for function type
_object.__init_by_constructor__ = __init_handle_by_constructor__
RETURN_SWITCH[ArgTypeCode.PACKED_FUNC_HANDLE] = _handle_return_func
RETURN_SWITCH[ArgTypeCode.MODULE_HANDLE] = _return_module
RETURN_SWITCH[ArgTypeCode.NDARRAY_HANDLE] = lambda x: _make_array(x.v_handle, False, True)
C_TO_PY_ARG_SWITCH[ArgTypeCode.PACKED_FUNC_HANDLE] = _wrap_arg_func(
    _handle_return_func, ArgTypeCode.PACKED_FUNC_HANDLE
)
C_TO_PY_ARG_SWITCH[ArgTypeCode.MODULE_HANDLE] = _wrap_arg_func(
    _return_module, ArgTypeCode.MODULE_HANDLE
)
C_TO_PY_ARG_SWITCH[ArgTypeCode.DLTENSOR_HANDLE] = lambda x: _make_array(x.v_handle, True, False)
C_TO_PY_ARG_SWITCH[ArgTypeCode.NDARRAY_HANDLE] = _wrap_arg_func(
    lambda x: _make_array(x.v_handle, False, True), ArgTypeCode.NDARRAY_HANDLE
)

_CLASS_MODULE = None
_CLASS_PACKED_FUNC = None
_CLASS_OBJECT_GENERIC = None
_FUNC_CONVERT_TO_OBJECT = None


def _set_class_module(module_class):
    """Initialize the module."""
    global _CLASS_MODULE
    _CLASS_MODULE = module_class


def _set_class_packed_func(packed_func_class):
    global _CLASS_PACKED_FUNC
    _CLASS_PACKED_FUNC = packed_func_class


def _set_class_object_generic(object_generic_class, func_convert_to_object):
    global _CLASS_OBJECT_GENERIC
    global _FUNC_CONVERT_TO_OBJECT
    _CLASS_OBJECT_GENERIC = object_generic_class
    _FUNC_CONVERT_TO_OBJECT = func_convert_to_object
