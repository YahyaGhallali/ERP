# -*- coding: utf-8 -*-
"""Nouveau modèle (créé par _name) : insea.absence.request

Une seule classe porte les deux volets du cahier des charges, distingués par
le champ `category` :
  * 'authorization' : autorisation de sortie / absence courte durée (en heures)
  * 'paid'          : congé payé annuel (en jours ouvrés, décompté du solde)
  * 'exceptional'   : congé exceptionnel pour événement familial
  * 'sick'          : congé maladie ordinaire

Organisation du fichier :
  1. Constantes
  2. Champs (couche "données")
  3. Champs calculés (@api.depends)
  4. Onchange / CRUD (create, write, unlink)
  5. Contraintes (@api.constrains + _sql_constraints)
  6. Workflow (boutons d'action de la machine à états)
  7. Méthodes utilitaires
"""
from datetime import datetime, time, timedelta

import pytz
from dateutil.relativedelta import relativedelta

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

# =============================================================================
# 1. CONSTANTES (règles de gestion paramétrées)
# =============================================================================
MAX_AUTHORIZATION_HOURS = 4.0        # 1/2 journée maximum
MAX_AUTHORIZATIONS_PER_MONTH = 3     # 3 autorisations / mois calendaire
MAX_AUTHORIZATION_HOURS_MONTH = 8.0  # ou 8 heures cumulées / mois
NOTICE_MIN_DAYS = 7                  # délai de prévenance (jours calendaires)
NOTICE_THRESHOLD_DAYS = 3            # ... pour les congés payés > 3 jours
HR_VALIDATION_SICK_THRESHOLD = 1     # maladie > 1 jour => validation RH

# Durées réglementaires des congés exceptionnels (jours) : (défaut, maximum)
FAMILY_EVENT_DAYS = {
    'marriage': (4, 4),
    'birth': (3, 3),
    'death': (3, 3),   # 2 à 3 jours selon le lien de parenté
}

# États "actifs" : demande en cours de validation ou déjà approuvée
ACTIVE_STATES = ('submitted', 'manager_approved', 'approved')

# Champs que le circuit de validation est seul autorisé à modifier
WORKFLOW_FIELDS = {
    'state', 'manager_user_id', 'manager_date', 'hr_user_id', 'hr_date',
    'refusal_reason', 'refused_by_id', 'refusal_date', 'submit_date',
}


class InseaAbsenceRequest(models.Model):
    _name = 'insea.absence.request'
    _description = "Demande d'absence / de congé"
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'start_datetime desc, id desc'
    _rec_name = 'name'

    # =========================================================================
    # 2. CHAMPS
    # =========================================================================
    name = fields.Char(
        string="Référence", required=True, copy=False, readonly=True,
        default=lambda self: _('Nouveau'))
    employee_id = fields.Many2one(
        'hr.employee', string="Employé", required=True, index=True,
        tracking=True, ondelete='cascade',
        default=lambda self: self.env.user.employee_id)
    user_id = fields.Many2one(
        'res.users', string="Utilisateur", related='employee_id.user_id',
        store=True, index=True)
    manager_id = fields.Many2one(
        'hr.employee', string="Responsable (N+1)",
        related='employee_id.parent_id', store=True, index=True)
    manager_user_related_id = fields.Many2one(
        'res.users', string="Utilisateur N+1",
        related='employee_id.parent_id.user_id', store=True)
    department_id = fields.Many2one(
        'hr.department', string="Département",
        related='employee_id.department_id', store=True)
    company_id = fields.Many2one(
        'res.company', string="Société",
        related='employee_id.company_id', store=True)

    category = fields.Selection([
        ('authorization', "Autorisation de sortie (courte durée)"),
        ('paid', "Congé payé annuel"),
        ('exceptional', "Congé exceptionnel (événement familial)"),
        ('sick', "Congé maladie"),
    ], string="Type de demande", required=True, default='authorization',
        tracking=True)

    # --- Volet 1 : autorisation de sortie (en heures) -----------------------
    authorization_reason = fields.Selection([
        ('medical', "Consultation médicale"),
        ('administrative', "Démarche administrative"),
        ('emergency', "Urgence personnelle"),
    ], string="Motif", tracking=True)
    authorization_date = fields.Date(string="Date de l'autorisation")
    hour_from = fields.Float(string="Heure de début", default=9.0)
    hour_to = fields.Float(string="Heure de fin", default=11.0)

    # --- Volet 2 : congés (en jours) ----------------------------------------
    family_event = fields.Selection([
        ('marriage', "Mariage (4 j)"),
        ('birth', "Naissance (3 j)"),
        ('death', "Décès (2 à 3 j)"),
    ], string="Événement familial", tracking=True)
    date_from = fields.Date(string="Du", tracking=True)
    date_to = fields.Date(string="Au", tracking=True)

    description = fields.Text(string="Commentaire / précisions")

    # --- Justificatifs ------------------------------------------------------
    attachment_ids = fields.Many2many(
        'ir.attachment', 'insea_absence_request_attachment_rel',
        'request_id', 'attachment_id', string="Justificatifs",
        help="Certificat médical, acte d'état civil... (PDF ou image)")

    # --- Workflow -----------------------------------------------------------
    state = fields.Selection([
        ('draft', "Brouillon"),
        ('submitted', "Soumis"),
        ('manager_approved', "Validé N+1 (attente RH)"),
        ('approved', "Validé"),
        ('refused', "Refusé"),
    ], string="Statut", default='draft', required=True, readonly=True,
        copy=False, tracking=True, index=True)
    submit_date = fields.Datetime(string="Soumis le", readonly=True, copy=False)
    manager_user_id = fields.Many2one(
        'res.users', string="Validé (N+1) par", readonly=True, copy=False)
    manager_date = fields.Datetime(string="Date validation N+1", readonly=True, copy=False)
    hr_user_id = fields.Many2one(
        'res.users', string="Validé (RH) par", readonly=True, copy=False)
    hr_date = fields.Datetime(string="Date validation RH", readonly=True, copy=False)
    refusal_reason = fields.Text(string="Motif du refus", readonly=True, copy=False)
    refused_by_id = fields.Many2one(
        'res.users', string="Refusé par", readonly=True, copy=False)
    refusal_date = fields.Datetime(string="Date du refus", readonly=True, copy=False)

    # =========================================================================
    # 3. CHAMPS CALCULÉS
    # =========================================================================
    duration_hours = fields.Float(
        string="Durée (heures)", compute='_compute_duration', store=True)
    duration_days = fields.Float(
        string="Durée (jours ouvrés)", compute='_compute_duration', store=True)
    duration_display = fields.Char(
        string="Durée", compute='_compute_duration', store=True)
    period_display = fields.Char(
        string="Période", compute='_compute_duration', store=True)

    start_datetime = fields.Datetime(
        string="Début", compute='_compute_datetimes', store=True, index=True)
    end_datetime = fields.Datetime(
        string="Fin", compute='_compute_datetimes', store=True)
    is_all_day = fields.Boolean(compute='_compute_datetimes', store=True)

    requires_attachment = fields.Boolean(
        string="Justificatif obligatoire", compute='_compute_rules_flags', store=True)
    requires_hr_validation = fields.Boolean(
        string="Validation RH requise", compute='_compute_rules_flags', store=True)
    has_attachment = fields.Boolean(compute='_compute_has_attachment')

    impacts_balance = fields.Boolean(
        string="Impacte le solde", compute='_compute_rules_flags', store=True)
    employee_leave_balance = fields.Float(
        string="Solde actuel (j)", related='employee_id.insea_leave_balance',
        groups='base.group_user')
    employee_leave_pending = fields.Float(
        string="En attente (j)", related='employee_id.insea_leave_pending',
        groups='base.group_user')

    # Champs techniques pour piloter l'affichage des boutons
    can_manager_approve = fields.Boolean(compute='_compute_user_rights')
    can_hr_approve = fields.Boolean(compute='_compute_user_rights')
    can_refuse = fields.Boolean(compute='_compute_user_rights')

    @api.depends('category', 'hour_from', 'hour_to', 'date_from', 'date_to',
                 'authorization_date', 'employee_id')
    def _compute_duration(self):
        def hhmm(value):
            h, m = int(value), int(round((value % 1) * 60))
            return f"{h:02d}:{m:02d}"

        for req in self:
            if req.category == 'authorization':
                hours = max(req.hour_to - req.hour_from, 0.0)
                req.duration_hours = hours
                req.duration_days = 0.0
                h, m = int(hours), int(round((hours % 1) * 60))
                req.duration_display = f"{h}h{m:02d}"
                req.period_display = (
                    f"{req.authorization_date.strftime('%d/%m/%Y')} "
                    f"{hhmm(req.hour_from)}–{hhmm(req.hour_to)}"
                    if req.authorization_date else False)
            else:
                days = 0.0
                if req.date_from and req.date_to and req.date_to >= req.date_from:
                    days = float(req._count_working_days(req.date_from, req.date_to))
                req.duration_hours = 0.0
                req.duration_days = days
                req.period_display = (
                    f"{req.date_from.strftime('%d/%m/%Y')} → {req.date_to.strftime('%d/%m/%Y')}"
                    if req.date_from and req.date_to else False)
                req.duration_display = _("%(days)s jour(s)", days=f"{days:g}")

    @api.depends('category', 'authorization_date', 'hour_from', 'hour_to',
                 'date_from', 'date_to', 'employee_id')
    def _compute_datetimes(self):
        """Dates/heures unifiées (UTC) : servent au contrôle de chevauchement
        et à la vue calendrier, quel que soit le type de demande."""
        for req in self:
            tz_name = req.employee_id.sudo().tz or self.env.user.tz or 'UTC'
            if req.category == 'authorization':
                req.is_all_day = False
                if req.authorization_date:
                    req.start_datetime = self._local_to_utc(
                        req.authorization_date, req.hour_from, tz_name)
                    req.end_datetime = self._local_to_utc(
                        req.authorization_date, req.hour_to, tz_name)
                else:
                    req.start_datetime = req.end_datetime = False
            else:
                req.is_all_day = True
                if req.date_from and req.date_to:
                    req.start_datetime = self._local_to_utc(req.date_from, 0.0, tz_name)
                    # fin de journée : 23:59:59
                    req.end_datetime = self._local_to_utc(
                        req.date_to, 24.0, tz_name) - timedelta(seconds=1)
                else:
                    req.start_datetime = req.end_datetime = False

    @api.depends('category', 'authorization_reason', 'duration_days')
    def _compute_rules_flags(self):
        for req in self:
            # Justificatif : motif médical, maladie, événement familial
            req.requires_attachment = (
                req.category in ('sick', 'exceptional')
                or (req.category == 'authorization'
                    and req.authorization_reason == 'medical'))
            # Validation RH : congés payés et exceptionnels, maladie > 1 jour.
            # Une autorisation de sortie (<= 4 h) est finalisée par le N+1.
            req.requires_hr_validation = (
                req.category in ('paid', 'exceptional')
                or (req.category == 'sick'
                    and req.duration_days > HR_VALIDATION_SICK_THRESHOLD))
            req.impacts_balance = req.category == 'paid'

    @api.depends('employee_id', 'category', 'name')
    def _compute_display_name(self):
        """Libellé lisible (calendrier, fil d'Ariane) : « Employé · Type »."""
        short = {'authorization': _("Autorisation"), 'paid': _("Congé payé"),
                 'exceptional': _("Congé exceptionnel"), 'sick': _("Maladie")}
        for req in self:
            if req.employee_id and req.category:
                req.display_name = f"{req.employee_id.name} · {short[req.category]}"
            else:
                req.display_name = req.name

    @api.depends('attachment_ids')
    def _compute_has_attachment(self):
        for req in self:
            req.has_attachment = bool(req.attachment_ids)

    @api.depends_context('uid')
    @api.depends('state', 'employee_id', 'requires_hr_validation')
    def _compute_user_rights(self):
        user = self.env.user
        is_hr = user.has_group('insea_hr_leave_custom.group_absence_hr')
        for req in self:
            own = req.employee_id.user_id == user
            is_manager = req.sudo().employee_id.parent_id.user_id == user
            req.can_manager_approve = (req.state == 'submitted' and not own
                                       and (is_manager or is_hr))
            req.can_hr_approve = (req.state == 'manager_approved' and not own and is_hr)
            req.can_refuse = req.can_manager_approve or req.can_hr_approve

    # =========================================================================
    # 4. ONCHANGE & CRUD
    # =========================================================================
    @api.onchange('category', 'family_event', 'date_from')
    def _onchange_family_event(self):
        """Proposer automatiquement la date de fin selon l'événement familial."""
        if self.category == 'exceptional' and self.family_event and self.date_from:
            default_days = FAMILY_EVENT_DAYS[self.family_event][0]
            self.date_to = self._add_working_days(self.date_from, default_days)

    @api.onchange('category')
    def _onchange_category(self):
        if self.category == 'authorization':
            self.family_event = False
        else:
            self.authorization_reason = False
            if self.category != 'exceptional':
                self.family_event = False

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', _('Nouveau')) == _('Nouveau'):
                vals['name'] = self.env['ir.sequence'].next_by_code(
                    'insea.absence.request') or _('Nouveau')
        records = super().create(vals_list)
        for rec in records:
            # Employé et N+1 suivent la demande (notifications du chatter)
            partners = (rec.employee_id.user_id.partner_id
                        | rec.sudo().employee_id.parent_id.user_id.partner_id)
            if partners:
                rec.message_subscribe(partner_ids=partners.ids)
        records._link_attachments()
        return records

    def write(self, vals):
        """Verrouillage : une demande soumise n'est plus modifiable par
        l'employé ; les champs du workflow ne bougent que via les boutons."""
        if not self.env.su and not self.env.context.get('insea_workflow'):
            is_hr = self.env.user.has_group('insea_hr_leave_custom.group_absence_hr')
            if not is_hr:
                if WORKFLOW_FIELDS & set(vals):
                    raise UserError(_(
                        "Le statut d'une demande ne peut être modifié que via "
                        "les boutons du circuit de validation."))
                business_fields = set(vals) - {'message_follower_ids', 'activity_ids'}
                # Un justificatif peut encore être ajouté tant que la demande
                # est en cours de validation ; le reste est verrouillé.
                if business_fields == {'attachment_ids'}:
                    locked = self.filtered(lambda r: r.state in ('approved', 'refused'))
                else:
                    locked = self.filtered(lambda r: r.state != 'draft')
                if locked and business_fields:
                    raise UserError(_(
                        "La demande %s n'est plus modifiable : elle a déjà été "
                        "soumise.", ', '.join(locked.mapped('name'))))
        res = super().write(vals)
        if 'attachment_ids' in vals:
            self._link_attachments()
        return res

    def unlink(self):
        is_hr = self.env.user.has_group('insea_hr_leave_custom.group_absence_hr')
        if not is_hr and any(r.state != 'draft' for r in self):
            raise UserError(_("Seules les demandes en brouillon peuvent être supprimées."))
        return super().unlink()

    # =========================================================================
    # 5. CONTRAINTES
    # =========================================================================
    _sql_constraints = [
        ('check_dates_order',
         "CHECK(date_from IS NULL OR date_to IS NULL OR date_to >= date_from)",
         "La date de fin doit être postérieure ou égale à la date de début."),
        ('check_hours_order',
         "CHECK(category != 'authorization' OR hour_to > hour_from)",
         "L'heure de fin doit être postérieure à l'heure de début."),
        ('check_hours_range',
         "CHECK(hour_from >= 0 AND hour_to <= 24)",
         "Les heures doivent être comprises entre 00:00 et 24:00."),
    ]

    @api.constrains('category', 'authorization_reason', 'authorization_date',
                    'family_event', 'date_from', 'date_to')
    def _check_required_fields(self):
        for req in self:
            if req.category == 'authorization':
                if not req.authorization_reason:
                    raise ValidationError(_("Le motif est obligatoire pour une autorisation de sortie."))
                if not req.authorization_date:
                    raise ValidationError(_("La date de l'autorisation est obligatoire."))
            else:
                if not req.date_from or not req.date_to:
                    raise ValidationError(_("Les dates de début et de fin du congé sont obligatoires."))
                if req.category == 'exceptional' and not req.family_event:
                    raise ValidationError(_("Veuillez préciser l'événement familial."))

    @api.constrains('category', 'hour_from', 'hour_to')
    def _check_authorization_duration(self):
        """Règle : autorisation de sortie <= 4 heures (1/2 journée)."""
        for req in self.filtered(lambda r: r.category == 'authorization'):
            if req.duration_hours > MAX_AUTHORIZATION_HOURS:
                raise ValidationError(_(
                    "Une autorisation de sortie ne peut pas dépasser %(max)s heures "
                    "(demandé : %(asked)s).",
                    max=MAX_AUTHORIZATION_HOURS, asked=req.duration_display))

    @api.constrains('category', 'authorization_date', 'hour_from', 'hour_to',
                    'state', 'employee_id')
    def _check_authorization_monthly_cap(self):
        """Règle : max 3 autorisations OU 8 heures cumulées par mois calendaire."""
        for req in self.filtered(lambda r: r.category == 'authorization'
                                 and r.authorization_date and r.state != 'refused'):
            month_start = req.authorization_date.replace(day=1)
            month_end = month_start + relativedelta(months=1, days=-1)
            others = self.sudo().search([
                ('id', '!=', req.id),
                ('employee_id', '=', req.employee_id.id),
                ('category', '=', 'authorization'),
                ('state', 'in', ACTIVE_STATES),
                ('authorization_date', '>=', month_start),
                ('authorization_date', '<=', month_end),
            ])
            count = len(others) + 1
            hours = sum(others.mapped('duration_hours')) + req.duration_hours
            if count > MAX_AUTHORIZATIONS_PER_MONTH:
                raise ValidationError(_(
                    "Plafond atteint : %(max)s autorisations de sortie maximum par mois "
                    "(%(month)s).", max=MAX_AUTHORIZATIONS_PER_MONTH,
                    month=month_start.strftime('%m/%Y')))
            if hours > MAX_AUTHORIZATION_HOURS_MONTH:
                raise ValidationError(_(
                    "Plafond atteint : %(max)s heures d'autorisation cumulées maximum "
                    "par mois (total avec cette demande : %(total)sh).",
                    max=MAX_AUTHORIZATION_HOURS_MONTH, total=f"{hours:g}"))

    @api.constrains('start_datetime', 'end_datetime', 'employee_id', 'state')
    def _check_overlap(self):
        """Règle : pas de chevauchement avec une demande approuvée ou en cours."""
        for req in self.filtered(lambda r: r.start_datetime and r.end_datetime
                                 and r.state != 'refused'):
            overlapping = self.sudo().search([
                ('id', '!=', req.id),
                ('employee_id', '=', req.employee_id.id),
                ('state', 'in', ACTIVE_STATES),
                ('start_datetime', '<', req.end_datetime),
                ('end_datetime', '>', req.start_datetime),
            ], limit=1)
            if overlapping:
                raise ValidationError(_(
                    "Chevauchement détecté avec la demande %(ref)s (%(type)s, statut : %(state)s).",
                    ref=overlapping.name,
                    type=dict(self._fields['category'].selection)[overlapping.category],
                    state=dict(self._fields['state'].selection)[overlapping.state]))

    @api.constrains('category', 'family_event', 'date_from', 'date_to')
    def _check_family_event_duration(self):
        """Règle : durées réglementaires des congés exceptionnels."""
        for req in self.filtered(lambda r: r.category == 'exceptional' and r.family_event):
            max_days = FAMILY_EVENT_DAYS[req.family_event][1]
            if req.duration_days > max_days:
                raise ValidationError(_(
                    "Le congé pour « %(event)s » est limité à %(max)s jours ouvrés "
                    "(demandé : %(asked)s).",
                    event=dict(self._fields['family_event'].selection)[req.family_event],
                    max=max_days, asked=f"{req.duration_days:g}"))

    @api.constrains('category', 'date_from', 'date_to')
    def _check_not_empty_leave(self):
        for req in self.filtered(lambda r: r.category != 'authorization'
                                 and r.date_from and r.date_to):
            if req.duration_days <= 0:
                raise ValidationError(_(
                    "La période choisie ne contient aucun jour ouvré "
                    "(week-end ou jour férié)."))

    # --- Contrôles exécutés au moment de la soumission / validation ---------
    def _check_balance(self):
        """Règle : solde disponible >= durée demandée (congés payés)."""
        for req in self.filtered('impacts_balance'):
            employee = req.employee_id.sudo()
            # On réserve les autres congés payés déjà en cours de validation
            other_pending = sum(employee.insea_request_ids.filtered(
                lambda r: r.id != req.id and r.category == 'paid'
                and r.state in ('submitted', 'manager_approved')).mapped('duration_days'))
            available = employee.insea_leave_balance - other_pending
            if available < req.duration_days:
                raise ValidationError(_(
                    "Solde de congés payés insuffisant : %(available)s jour(s) disponible(s) "
                    "pour %(asked)s jour(s) demandé(s).",
                    available=f"{available:g}", asked=f"{req.duration_days:g}"))

    def _check_notice_period(self):
        """Règle : congé payé > 3 j => soumis au moins 7 jours calendaires avant."""
        today = fields.Date.context_today(self)
        for req in self.filtered(lambda r: r.category == 'paid'):
            if (req.duration_days > NOTICE_THRESHOLD_DAYS
                    and (req.date_from - today).days < NOTICE_MIN_DAYS):
                raise ValidationError(_(
                    "Délai de prévenance non respecté : un congé payé de plus de %(threshold)s "
                    "jours doit être demandé au moins %(notice)s jours calendaires à l'avance "
                    "(début au plus tôt le %(earliest)s).",
                    threshold=NOTICE_THRESHOLD_DAYS, notice=NOTICE_MIN_DAYS,
                    earliest=(today + timedelta(days=NOTICE_MIN_DAYS)).strftime('%d/%m/%Y')))

    def _check_attachment_before_final(self):
        """Règle : pas de validation finale sans justificatif quand il est requis."""
        for req in self:
            if req.requires_attachment and not req.attachment_ids:
                raise ValidationError(_(
                    "La demande %s ne peut pas être validée : un justificatif "
                    "(certificat médical, acte d'état civil...) doit être joint.", req.name))

    # =========================================================================
    # 6. WORKFLOW (machine à états)
    # =========================================================================
    def action_submit(self):
        """Brouillon -> Soumis : exécution des contrôles de cohérence."""
        for req in self:
            if req.state != 'draft':
                raise UserError(_("Seule une demande en brouillon peut être soumise."))
            if req.employee_id.user_id != self.env.user and not self.env.user.has_group(
                    'insea_hr_leave_custom.group_absence_manager'):
                raise AccessError(_("Vous ne pouvez soumettre que vos propres demandes."))
        self._check_notice_period()
        self._check_balance()
        self.with_context(insea_workflow=True).write({
            'state': 'submitted',
            'submit_date': fields.Datetime.now(),
        })
        for req in self:
            manager_user = req.sudo().employee_id.parent_id.user_id
            if manager_user:
                req.activity_schedule(
                    'mail.mail_activity_data_todo', user_id=manager_user.id,
                    summary=_("Demande d'absence à valider"),
                    note=_("%(emp)s : %(type)s (%(dur)s)", emp=req.employee_id.name,
                           type=dict(self._fields['category'].selection)[req.category],
                           dur=req.duration_display))
            req.message_post(body=_("Demande soumise pour validation."))
        return True

    def action_manager_approve(self):
        """Soumis -> Validé N+1 (ou Validé si autorisation de sortie)."""
        for req in self:
            if not req.can_manager_approve:
                raise AccessError(_(
                    "Seul le responsable hiérarchique direct (ou les RH) peut valider "
                    "cette demande, et uniquement à l'état « Soumis »."))
            if not req.requires_hr_validation:
                # Validation du manager = validation finale
                req._check_attachment_before_final()
                new_state = 'approved'
            else:
                new_state = 'manager_approved'
            req.with_context(insea_workflow=True).write({
                'state': new_state,
                'manager_user_id': self.env.user.id,
                'manager_date': fields.Datetime.now(),
            })
            req.activity_feedback(['mail.mail_activity_data_todo'])
            if new_state == 'approved':
                req._notify_employee(_("Votre demande %s a été validée par votre responsable.", req.name))
            else:
                req._notify_employee(_(
                    "Votre demande %s a été validée par votre responsable ; "
                    "elle est transmise au service RH.", req.name))
        return True

    def action_hr_approve(self):
        """Validé N+1 -> Validé : validation RH, imputation définitive du solde."""
        for req in self:
            if not req.can_hr_approve:
                raise AccessError(_(
                    "Seul un Responsable RH peut effectuer la validation finale, "
                    "après la validation du N+1."))
        self._check_attachment_before_final()
        self._check_balance()
        self.with_context(insea_workflow=True).write({
            'state': 'approved',
            'hr_user_id': self.env.user.id,
            'hr_date': fields.Datetime.now(),
        })
        for req in self:
            msg = _("Votre demande %s a été validée par le service RH.", req.name)
            if req.impacts_balance:
                msg += " " + _("%s jour(s) ont été déduits de votre solde.",
                               f"{req.duration_days:g}")
            req._notify_employee(msg)
        return True

    def action_open_refuse_wizard(self):
        """Ouvre l'assistant de saisie du motif de refus (obligatoire)."""
        self.ensure_one()
        if not self.can_refuse:
            raise AccessError(_("Vous n'êtes pas autorisé à refuser cette demande."))
        return {
            'type': 'ir.actions.act_window',
            'name': _("Motif du refus"),
            'res_model': 'insea.absence.refuse.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_request_id': self.id},
        }

    def _do_refuse(self, reason):
        """Appelé par l'assistant : Soumis / Validé N+1 -> Refusé."""
        if not reason or not reason.strip():
            raise UserError(_("Le motif du refus est obligatoire."))
        for req in self:
            if not req.can_refuse:
                raise AccessError(_("Vous n'êtes pas autorisé à refuser cette demande."))
        self.with_context(insea_workflow=True).write({
            'state': 'refused',
            'refusal_reason': reason,
            'refused_by_id': self.env.user.id,
            'refusal_date': fields.Datetime.now(),
        })
        for req in self:
            req.activity_unlink(['mail.mail_activity_data_todo'])
            req._notify_employee(_(
                "Votre demande %(ref)s a été refusée.\nMotif : %(reason)s",
                ref=req.name, reason=reason))
        return True

    def action_reset_to_draft(self):
        """Refusé -> Brouillon : l'employé peut corriger et soumettre à nouveau."""
        for req in self:
            if req.state != 'refused':
                raise UserError(_("Seule une demande refusée peut être remise en brouillon."))
            if req.employee_id.user_id != self.env.user and not self.env.user.has_group(
                    'insea_hr_leave_custom.group_absence_hr'):
                raise AccessError(_("Seul l'employé concerné peut reprendre sa demande."))
        self.with_context(insea_workflow=True).write({
            'state': 'draft',
            'manager_user_id': False, 'manager_date': False,
            'hr_user_id': False, 'hr_date': False,
        })
        return True

    # =========================================================================
    # 7. UTILITAIRES
    # =========================================================================
    def _link_attachments(self):
        """Rattache les justificatifs à la demande (res_model/res_id) : leurs
        droits d'accès suivent alors ceux de la demande (manager, RH...)."""
        for req in self:
            req.attachment_ids.sudo().filtered(lambda a: not a.res_id).write({
                'res_model': self._name, 'res_id': req.id})

    def _notify_employee(self, body):
        """Notification de l'employé (boîte de réception Odoo + e-mail)."""
        self.ensure_one()
        partner = self.employee_id.user_id.partner_id
        self.message_post(
            body=body, message_type='comment', subtype_xmlid='mail.mt_comment',
            partner_ids=partner.ids if partner else [])

    @api.model
    def _local_to_utc(self, day, hours, tz_name):
        """Date + heure locale (ex : 9.5 = 09:30) -> datetime UTC naïf (stockage Odoo)."""
        local_dt = datetime.combine(day, time.min) + timedelta(hours=hours)
        tz = pytz.timezone(tz_name or 'UTC')
        return tz.localize(local_dt).astimezone(pytz.UTC).replace(tzinfo=None)

    def _get_public_holidays(self, date_from, date_to):
        """Jours fériés : congés globaux du calendrier de travail (configurés
        par les RH dans Odoo : Configuration > Jours fériés)."""
        calendar = (self.employee_id.sudo().resource_calendar_id
                    or self.env.company.resource_calendar_id)
        leaves = self.env['resource.calendar.leaves'].sudo().search([
            ('resource_id', '=', False),
            ('calendar_id', 'in', [calendar.id, False]),
            ('date_from', '<=', datetime.combine(date_to, time.max)),
            ('date_to', '>=', datetime.combine(date_from, time.min)),
        ])
        holidays = set()
        for leave in leaves:
            day = leave.date_from.date()
            while day <= leave.date_to.date():
                holidays.add(day)
                day += timedelta(days=1)
        return holidays

    def _count_working_days(self, date_from, date_to):
        """Jours ouvrés : du lundi au vendredi, hors jours fériés."""
        holidays = self._get_public_holidays(date_from, date_to)
        days, day = 0, date_from
        while day <= date_to:
            if day.weekday() < 5 and day not in holidays:
                days += 1
            day += timedelta(days=1)
        return days

    def _add_working_days(self, date_from, nb_days):
        """Date de fin pour `nb_days` jours ouvrés à partir de `date_from`."""
        day, counted = date_from, 0
        holidays = self._get_public_holidays(date_from, date_from + timedelta(days=nb_days * 3))
        while True:
            if day.weekday() < 5 and day not in holidays:
                counted += 1
                if counted == nb_days:
                    return day
            day += timedelta(days=1)
