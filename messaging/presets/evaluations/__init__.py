"""Native evaluation documents synchronized by their owning application."""
from importlib import import_module
from pathlib import Path
from messaging.chatbot.doctype.ai_evaluation_case_template.synchronization import register_evaluations


def definitions():
    return tuple(import_module(__name__ + '.' + file.stem).definition()
        for file in sorted(Path(__file__).parent.glob('*.py')) if file.name != '__init__.py')


def register():
    return register_evaluations(app='messaging', evaluations=definitions())
