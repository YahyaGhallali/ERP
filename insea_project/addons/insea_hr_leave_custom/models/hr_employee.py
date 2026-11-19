# -*- coding: utf-8 -*-
"""Extension (héritage par _inherit) du modèle standard Odoo hr.employee.

On ajoute à l'employé existant :
  * sa date d'embauche (base de calcul de l'acquisition des congés),
  * un compteur de congés payés calculé (acquis / pris / en attente / solde),
  * le lien vers ses demandes d'absence.
"""
from dateutil.relativedelta import relativedelta

from odoo import api, fields, models

# Règle de gestion : 1,5 jour de congé payé acquis par mois de travail effectif
MONTHLY_ACCRUAL_DAYS = 1.5


class HrEmployee(models.Model):
    _inherit = 'hr.employee'

    insea_hire_date = fields.Date(
        string="Date d'embauche",
        groups="hr.group_hr_user",
        help="Date de début du travail effectif. Sert au calcul de "
             "l'acquisition mensuelle des congés payés (1,5 j / mois).")
    insea_initial_balance = fields.Float(
        string="Report / ajustement (jours)",
        groups="hr.group_hr_user",
        help="Jours reportés d'une période précédente ou ajustement manuel "
             "saisi par le service RH (peut être négatif).")
    insea_request_ids = fields.One2many(
        'insea.absence.request', 'employee_id', string="Demandes d'absence",
        groups="hr.group_hr_user")

    insea_months_worked = fields.Integer(
        string="Mois de travail effectif", compute='_compute_leave_balance',
        groups="hr.group_hr_user")
    insea_leave_accrued = fields.Float(
        string="Congés acquis (j)", compute='_compute_leave_balance',
        groups="hr.group_hr_user")
    insea_leave_taken = fields.Float(
        string="Congés pris (j)", compute='_compute_leave_balance',
        groups="hr.group_hr_user")
    insea_leave_pending = fields.Float(
        string="Congés en attente (j)", compute='_compute_leave_balance',
        groups="hr.group_hr_user",
        help="Congés payés soumis mais pas encore validés par les RH.")
    insea_leave_balance = fields.Float(
        string="Solde congés payés (j)", compute='_compute_leave_balance',
        groups="hr.group_hr_user",
        help="Acquis + report - pris (demandes validées).")
    insea_request_count = fields.Integer(
        compute='_compute_request_count', groups="hr.group_hr_user")

    @api.depends('insea_hire_date', 'insea_initial_balance',
                 'insea_request_ids.state', 'insea_request_ids.category',
                 'insea_request_ids.duration_days')
    def _compute_leave_balance(self):
        today = fields.Date.context_today(self)
        for employee in self:
            # 1. Acquisition : 1,5 j par mois complet travaillé
            months = 0
            if employee.insea_hire_date and employee.insea_hire_date <= today:
                delta = relativedelta(today, employee.insea_hire_date)
                months = delta.years * 12 + delta.months
            accrued = months * MONTHLY_ACCRUAL_DAYS

            # 2. Consommation : seules les demandes de congé payé comptent
            paid = employee.insea_request_ids.filtered(lambda r: r.category == 'paid')
            taken = sum(paid.filtered(lambda r: r.state == 'approved').mapped('duration_days'))
            pending = sum(paid.filtered(
                lambda r: r.state in ('submitted', 'manager_approved')).mapped('duration_days'))

            employee.insea_months_worked = months
            employee.insea_leave_accrued = accrued
            employee.insea_leave_taken = taken
            employee.insea_leave_pending = pending
            employee.insea_leave_balance = accrued + employee.insea_initial_balance - taken

    @api.depends('insea_request_ids')
    def _compute_request_count(self):
        for employee in self:
            employee.insea_request_count = len(employee.insea_request_ids)

    def action_open_insea_requests(self):
        """Bouton intelligent (smart button) sur la fiche employé."""
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id(
            'insea_hr_leave_custom.action_absence_request_all')
        action['domain'] = [('employee_id', '=', self.id)]
        action['context'] = {'default_employee_id': self.id}
        return action
