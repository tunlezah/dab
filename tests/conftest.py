import pytest
from unittest.mock import AsyncMock, MagicMock, PropertyMock


@pytest.fixture
def sample_mux_data():
    return {
        "ensemble": {"label": "ABC NSW DAB", "id": "0x1001", "ecc": "0xE0"},
        "services": [
            {
                "sid": "0x1301",
                "label": "triple j",
                "dls_label": "Now Playing: Test Song - Test Artist",
                "mode": "DAB+",
                "programType": 10,
                "language": 0,
                "components": [
                    {
                        "componentnr": 0,
                        "primary": True,
                        "transportmode": "Audio",
                        "ascty": "DAB+",
                        "subchannel": {
                            "bitrate": 128,
                            "protection": "EEP 3-A",
                        },
                    }
                ],
            },
            {
                "sid": "0x1302",
                "label": "Double J",
                "dls_label": "",
                "mode": "DAB+",
                "components": [
                    {
                        "transportmode": "Audio",
                        "ascty": "DAB+",
                        "subchannel": {"bitrate": 80},
                    }
                ],
            },
        ],
        "demodulator": {"snr": 15.2, "frequencycorrection": 0},
    }


@pytest.fixture
def sample_station():
    return {
        "id": "0x1301",
        "name": "triple j",
        "ensemble": "ABC NSW DAB",
        "channel": "9A",
        "bitrate": 128,
        "mode": "DAB+",
        "dls": "Now Playing: Test Song",
    }
