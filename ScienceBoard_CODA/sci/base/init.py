import sys
import os
import urllib.request

from typing import TYPE_CHECKING

sys.dont_write_bytecode = True
if TYPE_CHECKING:
    from ..vm.vmanager import VManager


def raw_download(url: str, path: str) -> bool:
    path = os.path.expanduser(path)
    urllib.request.urlretrieve(url, path)
    return True

def raw_touch(text: str, path: str) -> bool:
    path = os.path.expanduser(path)
    with open(path, mode="w") as writable:
        writable.write(text)
    return True

def vm_download(url: str, path: str, manager: "VManager") -> bool:
    filename = os.path.split(path)[1]
    temp_path = os.path.join(manager.temp_dir, filename)
    if raw_download(url, temp_path):
        # do not use manager.write_file because it only supports textual files
        return manager._vmrun("CopyFileFromHostToGuest", temp_path, path)[1]

def vm_touch(text: str, path: str, manager: "VManager") -> bool:
    return manager.write_file(path, text)
