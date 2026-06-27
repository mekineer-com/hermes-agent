from gateway.platforms.whatsapp_common import WhatsAppBehaviorMixin


class _Adapter(WhatsAppBehaviorMixin):
    name = "whatsapp"

    def __init__(self):
        self._dm_policy = "allowlist"
        self._allow_from = {"15133278228"}
        self._group_policy = "open"
        self._group_allow_from = set()


def test_normalize_whatsapp_id_strips_device_suffix():
    assert (
        WhatsAppBehaviorMixin._normalize_whatsapp_id("15133278228:45@s.whatsapp.net")
        == "15133278228@s.whatsapp.net"
    )


def test_dm_allowlist_matches_lid_alias_shape():
    adapter = _Adapter()
    assert adapter._is_dm_allowed("+15133278228:45@s.whatsapp.net")
    assert not adapter._is_dm_allowed("999@s.whatsapp.net")
