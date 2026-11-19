# -*- coding: utf-8 -*-
"""Assistant (modèle transitoire) : saisie obligatoire du motif de refus."""
from odoo import fields, models


class InseaAbsenceRefuseWizard(models.TransientModel):
    _name = 'insea.absence.refuse.wizard'
    _description = "Assistant de refus d'une demande d'absence"

    request_id = fields.Many2one(
        'insea.absence.request', string="Demande", required=True, readonly=True)
    employee_id = fields.Many2one(related='request_id.employee_id')
    reason = fields.Text(string="Motif du refus", required=True)

    def action_confirm(self):
        self.ensure_one()
        self.request_id._do_refuse(self.reason)
        return {'type': 'ir.actions.act_window_close'}
