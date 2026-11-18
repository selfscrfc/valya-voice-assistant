"""Live CoreAudio inventory, independent of PortAudio's cached device indices."""
import ctypes as C
from dataclasses import dataclass


def fourcc(value):
    return int.from_bytes(value.encode('ascii'), 'big')


class Address(C.Structure):
    _fields_ = [('selector', C.c_uint32), ('scope', C.c_uint32), ('element', C.c_uint32)]


@dataclass(frozen=True)
class AudioDevice:
    id: int
    name: str
    manufacturer: str
    builtin: bool


class MacAudio:
    def __init__(self):
        self.ca = C.CDLL('/System/Library/Frameworks/CoreAudio.framework/CoreAudio')
        self.cf = C.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
        self.ca.AudioObjectGetPropertyDataSize.argtypes = [C.c_uint32, C.POINTER(Address), C.c_uint32, C.c_void_p, C.POINTER(C.c_uint32)]
        self.ca.AudioObjectGetPropertyData.argtypes = [C.c_uint32, C.POINTER(Address), C.c_uint32, C.c_void_p, C.POINTER(C.c_uint32), C.c_void_p]
        self.cf.CFStringGetCString.argtypes = [C.c_void_p, C.c_void_p, C.c_long, C.c_uint32]
        self.cf.CFStringGetCString.restype = C.c_bool
        self.cf.CFRelease.argtypes = [C.c_void_p]

    def read(self, device, selector, scope='glob'):
        address = Address(fourcc(selector), fourcc(scope), 0)
        size = C.c_uint32()
        status = self.ca.AudioObjectGetPropertyDataSize(device, C.byref(address), 0, None, C.byref(size))
        if status:
            raise OSError(f'CoreAudio {selector}: {status}')
        buffer = C.create_string_buffer(size.value)
        status = self.ca.AudioObjectGetPropertyData(device, C.byref(address), 0, None, C.byref(size), buffer)
        if status:
            raise OSError(f'CoreAudio {selector}: {status}')
        return buffer.raw[:size.value]

    def string(self, device, selector):
        pointer = C.c_void_p.from_buffer_copy(self.read(device, selector))
        if not pointer.value:
            return ''
        try:
            buffer = C.create_string_buffer(4096)
            if not self.cf.CFStringGetCString(pointer, buffer, len(buffer), 0x08000100):
                raise OSError('CoreAudio: cannot decode device name')
            return buffer.value.decode('utf-8')
        finally:
            self.cf.CFRelease(pointer)

    def devices(self):
        raw = self.read(1, 'dev#')
        ids = (C.c_uint32 * (len(raw) // 4)).from_buffer_copy(raw)
        result = []
        for device in ids:
            try:
                if not self.read(device, 'stm#', 'inpt'):
                    continue
                transport = C.c_uint32.from_buffer_copy(self.read(device, 'tran')).value
                result.append(AudioDevice(device, self.string(device, 'lnam'), self.string(device, 'lmak'),
                                          transport == fourcc('bltn')))
            except OSError:
                # Devices may disappear while CoreAudio is enumerating them.
                continue
        return tuple(sorted(result, key=lambda d: d.id))


def automatic_candidates(options, hardware):
    """Prefer DJI hardware, then built-in input. Never select arbitrary/virtual inputs."""
    ranked = []
    for index, name in options:
        matches = [d for d in hardware if d.name == name]
        dji = 'dji' in name.casefold() or any('dji' in d.manufacturer.casefold() for d in matches)
        builtin = any(d.builtin for d in matches)
        if dji or builtin:
            ranked.append((0 if dji else 1, name, index))
    return [(index, name) for _, name, index in sorted(ranked)]
