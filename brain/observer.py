from tools.screenshot_tool import capture_frame
from tools.vision import Vision


class Observer:

    def __init__(self):

        self.vision = Vision()

    def observe(self, prompt):

        image = capture_frame()

        return self.vision.look(
            image,
            prompt
        )


observer = Observer()