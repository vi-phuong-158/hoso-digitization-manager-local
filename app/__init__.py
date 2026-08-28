"""Pipeline số hóa hồ sơ đảng viên (MVP, DEV-locked).

Xem AGENTS.md để biết hợp đồng vận hành. Module trong package này KHÔNG được
hard-code provider/model; mọi lời gọi model đi qua app.vision_adapter.
"""

from .release import APP_VERSION

__version__ = APP_VERSION
