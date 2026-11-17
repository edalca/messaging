"""Initial Bot Flow declaration; provisioned explicitly from Desk."""
from messaging.chatbot.doctype.bot_flow_preset_language.bot_flow_preset_language import add_language
from messaging.chatbot.doctype.bot_flow_presets.bot_flow_presets import save_preset


def register():
    save_preset(
        key='messaging_registration',
        name='Default Registration',
        process='messaging.registration',
        inputs={'name': {
            'parameter': {'code': 'name', 'label': 'Name', 'parameter_type': 'Name'},
            'capture_order': 1,
        }},
        outcomes={'registered': {'mode': 'template'}},
        priority=1,
        show_in_menu=False,
        menu_order=0,
    )
    add_language(
        preset='messaging_registration',
        language='es',
        semantic_description='Completar el registro del contacto.',
        input_initial_message='Vamos a completar tu registro.',
        input_continuation_message='Completemos los datos pendientes.',
        parameters={'name': {'request_phrase': '¿Cuál es tu nombre?'}},
        outcomes={'registered': {
            'template_name': 'Initial Registration',
            'body': 'Tu registro está completo.',
        }},
    )
    add_language(
        preset='messaging_registration',
        language='en',
        semantic_description='Complete contact registration.',
        input_initial_message='Let us complete your registration.',
        input_continuation_message='Let us complete the remaining details.',
        parameters={'name': {'request_phrase': 'What is your name?'}},
        outcomes={'registered': {
            'template_name': 'Initial Registration',
            'body': 'Your registration is complete.',
        }},
    )
    return 'messaging_registration'
