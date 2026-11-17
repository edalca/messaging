"""Exact decimal answers; grouping separators and mixed formats are rejected."""
from decimal import Decimal, InvalidOperation
import re

from messaging.chatbot.doctype.bot_parameter.parameters.base import NativeType, Capture


class DecimalType(NativeType):
    input_hint = 'Use a decimal point without thousands separators, for example 2.50.'

    def validate(self, parameter, value):
        if type(value) in (str, int, Decimal) and (type(value) is not str or re.fullmatch(r'[+-]?[0-9]+(?:\.[0-9]+)?', value)):
            try:
                number = Decimal(value)
                if number.is_finite():
                    return True, number
            except InvalidOperation:
                pass
        return False, None

    def capture(self, parameter, text, *, multiple=False):
        from messaging.chatbot.doctype.bot_settings.language import current_language, resolve_process_language
        language = (current_language() or resolve_process_language()).split('-')[0]
        # Semicolons separate multiple answers; commas are never list separators.
        parts = text.strip().split(';') if multiple else [text.strip()]
        values = []
        for part in parts:
            part = part.strip()
            if ',' in part:
                if language != 'es' or not re.fullmatch(r'[+-]?[0-9]+,[0-9]+', part):
                    return Capture('INVALID')
                # A three-digit comma suffix can also be thousands grouping.
                if len(part.rsplit(',', 1)[1]) == 3:
                    return Capture('INVALID')
                part = part.replace(',', '.')
            valid, value = self.validate(parameter, part)
            if not valid:
                return Capture('INVALID')
            values.append(value)
        return Capture('VALID', tuple(values))
