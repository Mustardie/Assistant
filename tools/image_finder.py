import cv2
import numpy as np
import pyautogui


def find_image(image_path, confidence=0.8):
    screenshot = pyautogui.screenshot()

    screenshot = cv2.cvtColor(
        np.array(screenshot),
        cv2.COLOR_RGB2BGR
    )

    template = cv2.imread(image_path)

    if template is None:
        print("Couldn't load template:", image_path)
        return None

    result = cv2.matchTemplate(
        screenshot,
        template,
        cv2.TM_CCOEFF_NORMED
    )

    _, max_val, _, max_loc = cv2.minMaxLoc(result)

    print(f"Match confidence: {max_val:.3f}")

    if max_val < confidence:
        return None

    h, w = template.shape[:2]

    x = max_loc[0] + w // 2
    y = max_loc[1] + h // 2

    return x, y