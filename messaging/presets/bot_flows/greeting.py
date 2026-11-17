"""Initial Bot Flow declaration; provisioned explicitly from Desk."""
from messaging.chatbot.doctype.bot_flow_preset_language.bot_flow_preset_language import add_language
from messaging.chatbot.doctype.bot_flow_presets.bot_flow_presets import save_preset


def register():
    save_preset(
        key='messaging_greeting',
        name='Default Greeting',
        process='messaging.greeting',
        inputs={},
        outcomes={'greeted': {'mode': 'template'}},
        priority=0,
        show_in_menu=False,
        menu_order=0,
    )
    add_language(
        preset='messaging_greeting',
        language='es',
        semantic_description='Saludar al contacto.',
        outcomes={'greeted': {
            'template_name': 'Initial Greeting',
            'body': '¡Hola! ¿En qué podemos ayudarte?',
        }},
    )
    add_language(
        preset='messaging_greeting',
        language='en',
        semantic_description='Greet the contact.',
        outcomes={'greeted': {
            'template_name': 'Initial Greeting',
            'body': 'Hello! How can we help you?',
        }},
    )
    return 'messaging_greeting'
