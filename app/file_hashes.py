import hashlib

from os import walk
from os.path import isdir, join


FILE_HASHES = []


def set_md5_hash(filepath: str, md5sum: str):
    hashed_key = hashlib.new("sha256", filepath.encode()).hexdigest()
    FILE_HASHES.append({hashed_key: md5sum})


def get_md5_hash(filepath: str):
    hashed_key = hashlib.new("sha256", filepath.encode()).hexdigest()
    for entry in FILE_HASHES:
        if hashed_key in entry:
            return entry[hashed_key]
    return None


def del_md5_hash(filepath: str) -> bool:
    md5sum = get_md5_hash(filepath)
    if md5sum:
        for entry in FILE_HASHES:
            if md5sum in entry:
                FILE_HASHES.remove(entry)
                return True
    return False


def set_md5_hashes(directory) -> int:
    global FILE_HASHES

    def _compute_md5(filepath, chunk_size=8192):
        hasher = hashlib.md5()
        with open(filepath, "rb") as file:
            for chunk in iter(lambda: file.read(chunk_size), b""):
                hasher.update(chunk)
        return hasher.hexdigest()

    if not isdir(directory):
        raise ValueError(f"Invalid directory path: {directory}")

    FILE_HASHES = []
    file_count = 0
    for root, dirs, files in walk(directory):
        dirs[:] = [d for d in dirs if not str(d).startswith(".")]

        for file in files:
            if not str(file).startswith("."):
                file_path = join(root, file)
                md5 = _compute_md5(file_path)
                set_md5_hash(file_path, md5)
                file_count += 1
    return file_count
