# assets

The Figma spec uses **Inter** (12px/700 for the placeholder, 13px/600 for
responses). This module doesn't bundle the font file — if `Inter` isn't
installed on the machine running Nova, Qt will silently substitute the
nearest system sans-serif, which will look close but not identical.

To match exactly:
1. Download Inter (https://rsms.me/inter/) and drop the `.ttf` files here,
   e.g. `ui/assets/Inter-Regular.ttf`, `ui/assets/Inter-Bold.ttf`.
2. Load them once at app startup, before creating `NovaOverlay`:

```python
from PySide6.QtGui import QFontDatabase
QFontDatabase.addApplicationFont("ui/assets/Inter-Regular.ttf")
QFontDatabase.addApplicationFont("ui/assets/Inter-Bold.ttf")
```
