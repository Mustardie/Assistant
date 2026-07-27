from tools.screenshot_tool import capture_frame
from tools.vision import Vision

vision = Vision()

print("Take your browser to the page you want to test...")
input("Press ENTER when ready.")

image = capture_frame()

prompt = """
You are looking at a computer screen.

The user wants to click the FIRST YouTube video visible.

Return ONLY what you see.

If you can identify the first video, describe its exact location.

If you can return a bounding box, return JSON like:

{
    "left": 100,
    "top": 200,
    "right": 600,
    "bottom": 500
}

Otherwise explain why you cannot.

Return ONLY the answer.
"""

result = vision.look(image, prompt)

print("\n========== VISION OUTPUT ==========")
print(result)
print("===================================")