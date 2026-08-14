"""
YouTube AI Strategist — V0.2

A terminal chat that talks to Qwen3 8B through Ollama.
Loads the user's YouTube knowledge files (knowledge/ folder)
into the model's system context on every request.

Run with:  python main.py
Exit with: exit, quit, or Ctrl+C
"""

import sys

import knowledge
import ollama_client
import prompts

# Windows fix: make the terminal show Unicode correctly
# (em-dashes, curly quotes, etc. in the model's replies).
sys.stdout.reconfigure(encoding="utf-8")


def main():
    print("=" * 56)
    print("  YOUTUBE AI STRATEGIST  (V0.2)")
    print(f"  Model: {ollama_client.MODEL_NAME}")
    print("=" * 56)

    # Step 1: load the user's knowledge files into memory.
    try:
        knowledge_text = knowledge.load_knowledge()
    except FileNotFoundError as error:
        print(f"\n[ERROR] {error}")
        return

    # Step 2: make sure Ollama is running and the model exists.
    try:
        ollama_client.verify_setup()
    except (ConnectionError, LookupError) as error:
        print(f"\n[ERROR] {error}")
        return

    print(f"\nKnowledge loaded: {len(knowledge_text):,} characters.")
    print("Ask me anything about your videos.")
    print("Type 'exit' or 'quit' to close the program.")

    # Conversation history. Starts with ONE system message that combines
    # the persona, the knowledge instructions, and the knowledge files.
    # Because the whole history is sent on every request, the model
    # has this knowledge on every single turn.
    system_content = (
        prompts.SYSTEM_PROMPT
        + "\n\n"
        + prompts.KNOWLEDGE_HEADER
        + "\n"
        + knowledge_text
    )
    messages = [
        {"role": "system", "content": system_content},
    ]

    try:
        while True:
            user_input = input("\nYou: ").strip()

            if user_input.lower() in ("exit", "quit"):
                print("Goodbye!")
                break

            if not user_input:
                continue

            # Add the user's message to the history.
            messages.append({"role": "user", "content": user_input})

            try:
                # Send the WHOLE history (system + past turns + this turn).
                print("\nStrategist: ", end="", flush=True)
                reply = ollama_client.send_messages(messages)
                print(reply)
            except Exception as error:
                print(f"\n[ERROR] Could not get a reply from the model:\n{error}")
                continue

            # Add the model's reply to the history so it can "remember".
            messages.append({"role": "assistant", "content": reply})
    except KeyboardInterrupt:
        print("\n\nGoodbye!")


if __name__ == "__main__":
    main()
