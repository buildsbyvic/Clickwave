"""Turn failures into a short message plus a 'how to fix' explanation.

friendly(error_or_text) -> (title, summary, fix)
"""

import zipfile

from .decode import NEEDS_WEB_MEDIA_EXTENSIONS

FORMATS = "WAV, MP3, OGG (Vorbis), M4A/AAC, FLAC or WMA"

_RULES = [
    # (substring(s) to look for, title, summary, fix)
    ((NEEDS_WEB_MEDIA_EXTENSIONS,), "OGG audio needs a free add-on",
     "This PC has no decoder for OGG Vorbis audio yet.",
     "Install Microsoft's free 'Web Media Extensions' from the Microsoft Store, then try again."),
    (("0xC00D36C4", "can't open this file"), "Unsupported file",
     "Windows doesn't recognize this audio format.",
     f"Use {FORMATS}. Audacity (free) or any online converter can turn other formats into WAV."),
    (("Opus audio",), "Opus audio isn't supported yet",
     "This OGG file contains Opus audio, which Clickwave can't decode yet.",
     "Convert it to WAV or MP3 (Audacity, free) and import that instead."),
    (("no audio track",), "No audio in this file",
     "The file doesn't contain any sound.",
     "Pick a file with audio. For a video, make sure it was recorded with audio turned on."),
    (("file is empty", "no audio decoded", "produced no audio", "no Vorbis audio"), "Empty or damaged file",
     "The file has no readable audio - it may be empty, cut short or damaged.",
     "Re-export or re-download it, then try again."),
    (("no usable sounds found in this Mechvibes pack",), "Mechvibes pack has no sounds",
     "The pack's config.json points to audio that's missing or unreadable.",
     "Make sure the folder (or zip) contains the sound file(s) named in config.json, then import again."),
    (("no audio files found",), "No audio files found",
     "That folder or zip doesn't contain any audio files.",
     f"Choose a folder/zip with {FORMATS} files, a GD clickpack, or a Mechvibes pack."),
    (("zip is too large",), "Zip is too large",
     "The zip unpacks to more than 400 MB, which is far bigger than any click pack.",
     "Extract it yourself and import just the sound folder with 'Import a pack folder'."),
    (("WinError 112", "No space left", "not enough space"), "Disk is full",
     "There isn't enough free space to save this.",
     "Free up some space on the drive that holds %APPDATA% and try again."),
    (("WinError 5", "WinError 32", "Permission denied", "Access is denied", "being used by another process"),
     "File is in use or protected",
     "Clickwave wasn't allowed to read or write a file.",
     "Close programs that may be using it (Explorer preview pane, an audio editor, OneDrive syncing), "
     "or check that the folder isn't read-only, then try again."),
    (("too large to load", "MemoryError"), "File is too large",
     "The recording is too big to load into memory.",
     "Trim it to the part with the key presses (a minute is plenty) and import that."),
]


def friendly(error, title="Something went wrong"):
    text = str(error) if error is not None else ""
    if isinstance(error, zipfile.BadZipFile):
        return ("Not a valid zip", "This file isn't a readable zip archive.",
                "Re-download the pack - the file may be incomplete - or extract it and use 'Import a pack folder'.")
    for needles, rtitle, summary, fix in _RULES:
        if any(n.lower() in text.lower() for n in needles):
            return rtitle, summary, fix
    return (title, text or "An unexpected error happened.",
            "If it keeps happening, open Settings > About > Log and include it when reporting the problem.")
