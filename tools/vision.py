import requests
import base64
from io import BytesIO


class Vision:
    def __init__(self):
        self.url = "http://localhost:11434/api/chat"
        self.model = "qwen2.5vl:7b"
        self._session = None

    def _get_session(self):
        if self._session is None:
            import requests
            self._session = requests.Session()
        return self._session

    def look(self, image, prompt="Describe everything you see."):

        buffer = BytesIO()

        image.save(buffer, format="PNG")

        image_base64 = base64.b64encode(
            buffer.getvalue()
        ).decode("utf-8")

        session = self._get_session()
        response = session.post(
            self.url,
            json={
                "model": self.model,
                "messages": [
                    {
                        "role": "system",
                        "content": """
                You are the vision system for a desktop AI assistant.

                Rules:
                - Answer ONLY the user's question.
                - Be concise.
                - Do NOT describe irrelevant parts of the screen.
                - Do NOT explain your reasoning.
                - If the answer is yes or no, reply with only yes or no plus one short sentence if needed.
                - If asked for a location, give only the location.
                - Keep answers under 30 words unless the user explicitly asks for a detailed description.
                """
                    },
                    {
                        "role": "user",
                        "content": prompt,
                        "images": [image_base64]
                    }
                ],
                "stream": False
            },
            timeout=300
        )

        return response.json()["message"]["content"]