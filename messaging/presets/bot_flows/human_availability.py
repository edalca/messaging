"""Initial Bot Flow declaration; provisioned explicitly from Desk."""
from messaging.chatbot.doctype.bot_flow_preset_language.bot_flow_preset_language import add_language
from messaging.chatbot.doctype.bot_flow_presets.bot_flow_presets import save_preset


def register():
    save_preset(
        key='messaging_human_availability',
        name='Default Human Support',
        process='messaging.human_availability',
        inputs={},
        outcomes={
            'filtered': {'mode': 'template'},
            'open': {'mode': 'template'},
            'closed': {'mode': 'template'},
        },
        priority=0,
        show_in_menu=False,
        menu_order=0,
    )
    add_language(
        preset='messaging_human_availability',
        language='es',
        semantic_description='Consultar si la atención humana está abierta.',
        outcomes={
            'filtered': {
                'template_name': 'Initial Human Support Filtered',
                'body': 'Hemos recibido tu solicitud de atención humana. Por ahora seguimos atendiéndote por este medio; si aún necesitas un agente, puedes volver a solicitarlo.',
            },
            'open': {
                'template_name': 'Initial Human Support Open',
                'body': 'La atención humana está abierta.',
            },
            'closed': {
                'template_name': 'Initial Human Support Closed',
                'body': 'La atención humana está cerrada en este momento.',
            },
        },
    )
    add_language(
        preset='messaging_human_availability',
        language='en',
        semantic_description='Check whether human support is open.',
        outcomes={
            'filtered': {
                'template_name': 'Initial Human Support Filtered',
                'body': 'We have received your request for human support. We will keep helping you here for now; if you still need an agent, you can request one again.',
            },
            'open': {
                'template_name': 'Initial Human Support Open',
                'body': 'Human support is open.',
            },
            'closed': {
                'template_name': 'Initial Human Support Closed',
                'body': 'Human support is currently closed.',
            },
        },
    )
    return 'messaging_human_availability'
