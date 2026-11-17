"""Whole affirmative or negative answers only, during directed capture."""
from messaging.chatbot.doctype.bot_parameter.parameters.base import NativeType, Capture


class BooleanType(NativeType):
    def validate(self, parameter, value):
        return (True, value) if type(value) is bool else (False, None)

    def capture(self, parameter, text, *, multiple=False):
        from messaging.chatbot.doctype.bot_settings.language import get_bot_settings
        # Recognition vocabulary belongs to the processor, not the translation catalogue.
        # Enabled native languages control which complete answers can be recognized.
        enabled = {row.language.split('-')[0] for row in get_bot_settings().chatbot_languages if row.enabled}
        answers = {}
        if 'es' in enabled:
            answers.update({'sí': True, 'si': True, 'no': False})
        if 'en' in enabled:
            answers.update({'yes': True, 'no': False})
        parts = text.strip().split(';') if multiple else [text.strip()]
        normalized = [part.strip().casefold() for part in parts]
        if any(part not in answers for part in normalized):
            return Capture('INVALID')
        return Capture('VALID', tuple(answers[part] for part in normalized))
