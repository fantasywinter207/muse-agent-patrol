"""Lazy secret sources. Never serialize credentials or include them in diagnostics."""
import ctypes
import json
import os
import stat


def credential_provider(config):
    def read():
        if os.name=="nt" and not config.credential_file:
            class Credential(ctypes.Structure):
                _fields_=[("Flags",ctypes.c_uint32),("Type",ctypes.c_uint32),("TargetName",ctypes.c_wchar_p),
                    ("Comment",ctypes.c_wchar_p),("LastWritten",ctypes.c_uint64),("CredentialBlobSize",ctypes.c_uint32),
                    ("CredentialBlob",ctypes.POINTER(ctypes.c_ubyte)),("Persist",ctypes.c_uint32),
                    ("AttributeCount",ctypes.c_uint32),("Attributes",ctypes.c_void_p),
                    ("TargetAlias",ctypes.c_wchar_p),("UserName",ctypes.c_wchar_p)]
            api=ctypes.WinDLL("Advapi32.dll",use_last_error=True)
            ptr=ctypes.POINTER(Credential)()
            api.CredReadW.argtypes=[ctypes.c_wchar_p,ctypes.c_uint32,ctypes.c_uint32,ctypes.POINTER(ctypes.POINTER(Credential))]
            api.CredReadW.restype=ctypes.c_int
            api.CredFree.argtypes=[ctypes.c_void_p]
            if not api.CredReadW(config.credential_target,1,0,ctypes.byref(ptr)):
                raise RuntimeError("Credential Manager entry unavailable")
            try:
                raw=ctypes.string_at(ptr.contents.CredentialBlob,ptr.contents.CredentialBlobSize)
                key=raw.decode("utf-16-le") if b"\x00" in raw else raw.decode("utf-8")
                return key.rstrip("\x00")
            finally:
                api.CredFree(ptr)
        if not config.credential_file:
            raise RuntimeError("explicit private credential_file required on this OS")
        flags=os.O_RDONLY|getattr(os,"O_NOFOLLOW",0)
        fd=os.open(config.credential_file,flags)
        with os.fdopen(fd,"r",encoding="utf-8") as f:
            info=os.fstat(f.fileno())
            if not stat.S_ISREG(info.st_mode) or (os.name=="posix" and (info.st_uid!=os.getuid() or info.st_mode&0o077)):
                raise RuntimeError("credential file must be owned by this user with mode 0600")
            value=json.load(f)
        if set(value)!={"apiKey"} or not isinstance(value["apiKey"],str) or not value["apiKey"]:
            raise RuntimeError("credential file has invalid shape")
        return value["apiKey"]
    return read
