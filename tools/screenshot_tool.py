def capture_frame():
    import mss
    from PIL import Image

    with mss.mss() as sct:
        screenshot = sct.grab(sct.monitors[1])

        image = Image.frombytes(
            "RGB",
            screenshot.size,
            screenshot.rgb
        )

        return image