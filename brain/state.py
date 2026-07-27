from dataclasses import dataclass, field


@dataclass
class AgentState:

    goal: str = ""

    history: list = field(default_factory=list)

    last_action: dict | None = None

    last_result: object = None

    finished: bool = False

    def reset(self):

        self.goal = ""

        self.history.clear()

        self.last_action = None

        self.last_result = None

        self.finished = False

    def add_action(self, action, result):

        self.last_action = action

        self.last_result = result

        self.history.append({
            "action": action,
            "result": result
        })