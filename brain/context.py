class Context:

    def __init__(self):

        self.reset()

    def reset(self):

        # Current goal
        self.goal = None

        # Browser
        self.current_app = None
        self.current_url = None
        self.current_page = None

        # User focus
        self.selected_item = None
        self.search_query = None

        # Execution
        self.last_action = None
        self.last_result = None

        # Vision
        self.last_screen = None

        # Arbitrary data
        self.data = {}