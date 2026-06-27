from gateway.platforms.whatsapp_common import WhatsAppBehaviorMixin


class _Adapter(WhatsAppBehaviorMixin):
    name = "whatsapp"

    def __init__(self):
        self._dm_policy = "allowlist"
        self._allow_from = {"12025550199"}
        self._group_policy = "open"
        self._group_allow_from = set()


def test_normalize_whatsapp_id_strips_device_suffix():
    assert (
        WhatsAppBehaviorMixin._normalize_whatsapp_id("12025550199:45@s.whatsapp.net")
        == "12025550199@s.whatsapp.net"
    )


def test_dm_allowlist_matches_lid_alias_shape():
    adapter = _Adapter()
    assert adapter._is_dm_allowed("+12025550199:45@s.whatsapp.net")
    assert not adapter._is_dm_allowed("999@s.whatsapp.net")
