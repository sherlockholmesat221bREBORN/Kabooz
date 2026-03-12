# quality.py
from enum import IntEnum


class Quality(IntEnum):
    # These numeric values are what Qobuz's API actually expects in the
    # format_id parameter. They were determined by reverse-engineering
    # the official clients — they're not documented publicly.
    MP3_320    = 5   # Lossy. 320kbps MP3.
    FLAC_16    = 6   # Lossless. 16-bit / 44.1kHz — standard CD quality.
    FLAC_24_96 = 7   # Hi-Res. 24-bit, up to 96kHz.
    HI_RES     = 27  # Hi-Res Max. 24-bit, up to 192kHz.

    @property
    def is_lossless(self) -> bool:
        # Because IntEnum members are integers, comparison operators work
        # naturally. Anything at or above FLAC_16 (6) is lossless.
        return self >= Quality.FLAC_16

    @property
    def is_hi_res(self) -> bool:
        return self >= Quality.FLAC_24_96

    @property
    def extension(self) -> str:
        """The file extension for audio saved at this quality tier."""
        return ".mp3" if self == Quality.MP3_320 else ".flac"

    @property
    def label(self) -> str:
        """A human-readable string suitable for display in a UI or log."""
        return {
            Quality.MP3_320:    "MP3 320kbps",
            Quality.FLAC_16:    "FLAC 16-bit / 44.1kHz",
            Quality.FLAC_24_96: "FLAC 24-bit / 96kHz",
            Quality.HI_RES:     "FLAC 24-bit / 192kHz",
        }[self]


# A list of all tiers from best to worst. This is useful later when
# implementing quality fallback logic — if HI_RES isn't available for a
# track, you iterate down this list until you find one that is.
QUALITY_DESCENDING = [
    Quality.HI_RES,
    Quality.FLAC_24_96,
    Quality.FLAC_16,
    Quality.MP3_320,
]
