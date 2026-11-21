# -*- coding: utf-8 -*-
{
    'name': 'INSEA - Autorisations d\'absence & Congés',
    'version': '18.0.1.0.0',
    'summary': 'Gestion intégrée des autorisations de sortie et des congés (RH)',
    'description': """
Mini-projet Systèmes Intégrés - INSEA 2026/2027.

Gestion dématérialisée des autorisations de sortie (4 h max, 3 par mois ou
8 h cumulées), des congés payés annuels (1,5 jour acquis par mois, contrôle du
solde), des congés exceptionnels pour événement familial et des congés
maladie, avec circuit de validation Brouillon, Soumis, Validation N+1,
Validation RH, et droits d'accès par profil (Employé, Manager, RH, Admin).
""",
    'author': 'Binôme DSE3 - INSEA',
    'category': 'Human Resources',
    'license': 'LGPL-3',
    'depends': ['hr', 'mail'],
    'data': [
        # 1. Sécurité (toujours en premier : groupes avant droits et règles)
        'security/security.xml',
        'security/ir.model.access.csv',
        # 2. Données de base & notifications
        'data/ir_sequence_data.xml',
        'data/mail_template_data.xml',
        'data/ir_cron_data.xml',
        # 3. Rapports PDF (QWeb)
        'report/absence_request_report.xml',
        # 4. Vues & assistants
        'wizard/refuse_wizard_views.xml',
        'wizard/cancel_wizard_views.xml',
        'views/absence_request_views.xml',
        'views/hr_employee_views.xml',
        'views/menus.xml',
    ],
    'demo': [
        'demo/demo_data.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'insea_hr_leave_custom/static/src/scss/insea_absence.scss',
        ],
    },
    'application': True,
    'installable': True,
}
