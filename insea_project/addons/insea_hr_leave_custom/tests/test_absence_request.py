# -*- coding: utf-8 -*-
"""Jeu de tests automatisés (à citer dans le rapport, partie "Tests et validation").

Lancement :
    odoo-bin -d <base_de_test> -i insea_hr_leave_custom --test-enable \
             --test-tags /insea_hr_leave_custom --stop-after-init
"""
import base64
from datetime import timedelta

from dateutil.relativedelta import relativedelta

from odoo import fields
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestAbsenceRequest(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.today = fields.Date.context_today(cls.env['insea.absence.request'])
        Users = cls.env['res.users'].with_context(no_reset_password=True)
        g_user = cls.env.ref('base.group_user')
        g_manager = cls.env.ref('insea_hr_leave_custom.group_absence_manager')
        g_hr = cls.env.ref('insea_hr_leave_custom.group_absence_hr')

        def make_user(login, groups):
            return Users.create({
                'name': login.capitalize(), 'login': login,
                'email': f'{login}@test.example', 'tz': 'Africa/Casablanca',
                'groups_id': [(6, 0, groups.ids)],
            })
        cls.user_hr = make_user('t_hr', g_user | g_hr)
        cls.user_manager = make_user('t_manager', g_user | g_manager)
        cls.user_emp = make_user('t_emp', g_user)
        cls.user_emp2 = make_user('t_emp2', g_user)
        cls.user_other = make_user('t_other', g_user)

        Emp = cls.env['hr.employee']
        cls.emp_hr = Emp.create({'name': 'RH', 'user_id': cls.user_hr.id})
        cls.emp_manager = Emp.create({
            'name': 'Manager', 'user_id': cls.user_manager.id, 'parent_id': cls.emp_hr.id,
            'insea_hire_date': cls.today - relativedelta(years=3)})
        cls.emp = Emp.create({
            'name': 'Employé', 'user_id': cls.user_emp.id, 'parent_id': cls.emp_manager.id,
            'tz': 'Africa/Casablanca',
            # 10 mois complets => 15 jours acquis
            'insea_hire_date': cls.today - relativedelta(months=10, days=2)})
        cls.emp2 = Emp.create({
            'name': 'Employé 2', 'user_id': cls.user_emp2.id, 'parent_id': cls.emp_manager.id,
            'insea_hire_date': cls.today - relativedelta(months=2, days=2)})  # 3 jours
        # Employé d'une autre équipe (manager = RH)
        cls.emp_other = Emp.create({
            'name': 'Autre', 'user_id': cls.user_other.id, 'parent_id': cls.emp_hr.id,
            'insea_hire_date': cls.today - relativedelta(years=1)})

    # ---------------------------------------------------------------- helpers
    def _monday(self, min_days):
        """Premier lundi situé au moins `min_days` jours après aujourd'hui."""
        day = self.today + timedelta(days=min_days)
        return day + timedelta(days=(7 - day.weekday()) % 7)

    def _request(self, user=None, **vals):
        user = user or self.user_emp
        employee = self.env['hr.employee'].search([('user_id', '=', user.id)], limit=1)
        vals.setdefault('employee_id', employee.id)
        return self.env['insea.absence.request'].with_user(user).create(vals)

    def _paid(self, start, nb_days, user=None):
        return self._request(user=user, category='paid', date_from=start,
                             date_to=start + timedelta(days=nb_days - 1))

    def _auth(self, day, h_from, h_to, reason='administrative', user=None):
        return self._request(user=user, category='authorization', authorization_reason=reason,
                             authorization_date=day, hour_from=h_from, hour_to=h_to)

    def _attachment(self, user=None):
        return self.env['ir.attachment'].with_user(user or self.user_emp).create({
            'name': 'justificatif.pdf', 'datas': base64.b64encode(b'%PDF-1.4 test'),
            'mimetype': 'application/pdf'})

    # --------------------------------------------------------- 1. calculs
    def test_01_accrual_and_balance(self):
        """1,5 j acquis par mois complet ; le solde baisse après validation RH."""
        self.assertEqual(self.emp.insea_months_worked, 10)
        self.assertEqual(self.emp.insea_leave_accrued, 15.0)
        self.assertEqual(self.emp.insea_leave_balance, 15.0)

        req = self._paid(self._monday(14), 3)          # lundi -> mercredi
        self.assertEqual(req.duration_days, 3)
        req.with_user(self.user_emp).action_submit()
        self.assertEqual(self.emp.insea_leave_pending, 3)
        req.with_user(self.user_manager).action_manager_approve()
        req.with_user(self.user_hr).action_hr_approve()
        self.assertEqual(req.state, 'approved')
        self.assertEqual(self.emp.insea_leave_taken, 3)
        self.assertEqual(self.emp.insea_leave_balance, 12.0)

    def test_02_working_days_exclude_weekend_and_holidays(self):
        monday = self._monday(14)
        req = self._paid(monday, 7)                     # lundi -> dimanche
        self.assertEqual(req.duration_days, 5, "Le week-end ne doit pas être décompté")
        # Jour férié le mercredi
        holiday_day = monday + timedelta(days=2)
        self.env['resource.calendar.leaves'].create({
            'name': 'Jour férié test',
            'date_from': fields.Datetime.to_datetime(holiday_day),
            'date_to': fields.Datetime.to_datetime(holiday_day) + timedelta(hours=23, minutes=59),
            'calendar_id': False, 'resource_id': False})
        req_new = self._request(category='sick', date_from=monday, date_to=monday + timedelta(days=4))
        self.assertEqual(req_new.duration_days, 4, "Le jour férié ne doit pas être décompté")

    # ------------------------------------------------ 2. autorisations
    def test_03_authorization_max_4_hours(self):
        with self.assertRaises(ValidationError):
            self._auth(self._monday(3), 8.0, 12.5)      # 4h30
        ok = self._auth(self._monday(3), 8.0, 12.0)     # 4h pile
        self.assertEqual(ok.duration_hours, 4.0)
        self.assertEqual(ok.duration_days, 0, "Une autorisation n'impacte pas le solde")

    def test_04_authorization_max_3_per_month(self):
        monday = self._monday(35).replace(day=1)
        days = [d for d in (monday + timedelta(days=i) for i in range(28)) if d.weekday() < 5]
        for day in days[:3]:
            self._auth(day, 9.0, 10.0).with_user(self.user_emp).action_submit()
        with self.assertRaises(ValidationError):
            self._auth(days[3], 9.0, 10.0)

    def test_05_authorization_max_8_hours_per_month(self):
        monday = self._monday(35).replace(day=1)
        days = [d for d in (monday + timedelta(days=i) for i in range(28)) if d.weekday() < 5]
        self._auth(days[0], 8.0, 12.0).with_user(self.user_emp).action_submit()   # 4h
        self._auth(days[1], 8.0, 11.0).with_user(self.user_emp).action_submit()   # 3h
        with self.assertRaises(ValidationError):
            self._auth(days[2], 8.0, 10.0)                                         # 9h > 8h

    def test_06_reason_required(self):
        with self.assertRaises(ValidationError):
            self._request(category='authorization', authorization_date=self._monday(3),
                          hour_from=9, hour_to=10)

    # ------------------------------------------------- 3. contrôles
    def test_07_overlap_forbidden(self):
        start = self._monday(14)
        self._paid(start, 3).with_user(self.user_emp).action_submit()
        with self.assertRaises(ValidationError):
            self._request(category='sick', date_from=start + timedelta(days=1),
                          date_to=start + timedelta(days=1))
        with self.assertRaises(ValidationError):
            self._auth(start + timedelta(days=2), 9.0, 10.0)
        # Un brouillon ne bloque pas les autres saisies
        self._paid(start + timedelta(days=7), 1)
        self._paid(start + timedelta(days=7), 1)

    def test_08_insufficient_balance(self):
        req = self._paid(self._monday(14), 5, user=self.user_emp2)   # solde = 3 j
        with self.assertRaises(ValidationError):
            req.with_user(self.user_emp2).action_submit()
        req2 = self._paid(self._monday(21), 3, user=self.user_emp2)   # 3 <= 3 : OK
        req2.with_user(self.user_emp2).action_submit()
        self.assertEqual(req2.state, 'submitted')

    def test_09_pending_requests_reserve_balance(self):
        self._paid(self._monday(14), 3, user=self.user_emp2).with_user(self.user_emp2).action_submit()
        req = self._paid(self._monday(28), 1, user=self.user_emp2)
        with self.assertRaises(ValidationError):
            req.with_user(self.user_emp2).action_submit()

    def test_10_notice_period(self):
        late = self._paid(self.today + timedelta(days=3), 6)   # > 3 j ouvrés, 3 j avant
        self.assertGreater(late.duration_days, 3)
        with self.assertRaises(ValidationError):
            late.with_user(self.user_emp).action_submit()
        short = self._paid(self._monday(1), 2)                 # <= 3 j : pas de délai
        short.with_user(self.user_emp).action_submit()
        self.assertEqual(short.state, 'submitted')

    def test_11_family_event_limits(self):
        start = self._monday(14)
        with self.assertRaises(ValidationError):
            self._request(category='exceptional', family_event='marriage',
                          date_from=start, date_to=start + timedelta(days=7))   # 6 j
        ok = self._request(category='exceptional', family_event='marriage',
                           date_from=start, date_to=start + timedelta(days=3))  # 4 j
        self.assertEqual(ok.duration_days, 4)
        self.assertTrue(ok.requires_attachment)

    # --------------------------------------------------- 4. workflow
    def test_12_authorization_final_at_manager(self):
        req = self._auth(self._monday(3), 9.0, 11.0)
        req.with_user(self.user_emp).action_submit()
        self.assertEqual(req.state, 'submitted')
        req.with_user(self.user_manager).action_manager_approve()
        self.assertEqual(req.state, 'approved', "Autorisation : validation N+1 finale")

    def test_13_paid_leave_needs_hr(self):
        req = self._paid(self._monday(14), 2)
        req.with_user(self.user_emp).action_submit()
        req.with_user(self.user_manager).action_manager_approve()
        self.assertEqual(req.state, 'manager_approved')
        with self.assertRaises(AccessError):
            req.with_user(self.user_manager).action_hr_approve()
        req.with_user(self.user_hr).action_hr_approve()
        self.assertEqual(req.state, 'approved')
        self.assertEqual(req.hr_user_id, self.user_hr)

    def test_14_attachment_blocks_final_validation(self):
        sick = self._request(category='sick', date_from=self._monday(7),
                             date_to=self._monday(7) + timedelta(days=2))
        self.assertTrue(sick.requires_hr_validation)
        sick.with_user(self.user_emp).action_submit()
        sick.with_user(self.user_manager).action_manager_approve()
        with self.assertRaises(ValidationError):
            sick.with_user(self.user_hr).action_hr_approve()
        # L'employé ajoute son certificat après soumission
        sick.with_user(self.user_emp).write({'attachment_ids': [(4, self._attachment().id)]})
        sick.with_user(self.user_hr).action_hr_approve()
        self.assertEqual(sick.state, 'approved')
        # Le justificatif est rattaché à la demande (lisible par les RH)
        self.assertEqual(sick.attachment_ids.res_model, 'insea.absence.request')
        sick.attachment_ids.with_user(self.user_hr).read(['name'])

    def test_15_medical_authorization_needs_attachment(self):
        req = self._auth(self._monday(3), 9.0, 10.0, reason='medical')
        req.with_user(self.user_emp).action_submit()
        with self.assertRaises(ValidationError):
            req.with_user(self.user_manager).action_manager_approve()

    def test_16_sick_one_day_final_at_manager(self):
        req = self._request(category='sick', date_from=self._monday(7), date_to=self._monday(7),
                            attachment_ids=[(6, 0, self._attachment().ids)])
        self.assertFalse(req.requires_hr_validation)
        req.with_user(self.user_emp).action_submit()
        req.with_user(self.user_manager).action_manager_approve()
        self.assertEqual(req.state, 'approved')

    def test_17_refusal_requires_reason(self):
        req = self._paid(self._monday(14), 2)
        req.with_user(self.user_emp).action_submit()
        Wizard = self.env['insea.absence.refuse.wizard'].with_user(self.user_manager)
        with self.assertRaises(UserError):
            req.with_user(self.user_manager)._do_refuse('   ')
        Wizard.create({'request_id': req.id, 'reason': 'Période chargée'}).action_confirm()
        self.assertEqual(req.state, 'refused')
        self.assertEqual(req.refusal_reason, 'Période chargée')
        self.assertEqual(req.refused_by_id, self.user_manager)
        # L'employé est notifié
        self.assertTrue(req.message_ids.filtered(
            lambda m: self.user_emp.partner_id in m.partner_ids and 'Période chargée' in m.body))
        # ... et peut corriger puis soumettre à nouveau
        req.with_user(self.user_emp).action_reset_to_draft()
        self.assertEqual(req.state, 'draft')

    def test_18_locked_after_submit(self):
        req = self._paid(self._monday(14), 2)
        req.with_user(self.user_emp).action_submit()
        with self.assertRaises(UserError):
            req.with_user(self.user_emp).write({'description': 'modification'})
        with self.assertRaises(UserError):
            req.with_user(self.user_emp).unlink()

    def test_19_employee_cannot_force_state(self):
        req = self._paid(self._monday(14), 2)
        with self.assertRaises(UserError):
            req.with_user(self.user_emp).write({'state': 'approved'})
        with self.assertRaises(AccessError):
            req.with_user(self.user_emp).action_manager_approve()

    def test_20_no_self_approval(self):
        req = self._paid(self._monday(14), 2, user=self.user_manager)
        req.with_user(self.user_manager).action_submit()
        with self.assertRaises(AccessError):
            req.with_user(self.user_manager).action_manager_approve()
        req.with_user(self.user_hr).action_manager_approve()   # son N+1 (RH)
        self.assertEqual(req.state, 'manager_approved')

    def test_21_other_manager_cannot_approve(self):
        other_req = self._paid(self._monday(14), 1, user=self.user_other)
        other_req.with_user(self.user_other).action_submit()
        with self.assertRaises(AccessError):
            other_req.with_user(self.user_manager).action_manager_approve()

    # ------------------------------------------------- 5. sécurité
    def test_22_record_rules(self):
        Req = self.env['insea.absence.request']
        r_emp = self._paid(self._monday(14), 1)
        r_emp2 = self._paid(self._monday(14), 1, user=self.user_emp2)
        r_other = self._paid(self._monday(14), 1, user=self.user_other)
        all_ids = (r_emp | r_emp2 | r_other).ids
        domain = [('id', 'in', all_ids)]

        self.assertEqual(Req.with_user(self.user_emp).search(domain), r_emp,
                         "L'employé ne voit que ses demandes")
        self.assertEqual(set(Req.with_user(self.user_manager).search(domain).ids),
                         {r_emp.id, r_emp2.id}, "Le manager voit son équipe")
        self.assertEqual(set(Req.with_user(self.user_hr).search(domain).ids), set(all_ids),
                         "Les RH voient tout")
        with self.assertRaises(AccessError):
            r_other.with_user(self.user_emp).read(['name'])

    def test_23_employee_cannot_create_for_colleague(self):
        with self.assertRaises(AccessError):
            self.env['insea.absence.request'].with_user(self.user_emp).create({
                'employee_id': self.emp2.id, 'category': 'paid',
                'date_from': self._monday(14), 'date_to': self._monday(14)})

    def test_24_employee_reads_own_balance(self):
        req = self._paid(self._monday(14), 1)
        self.assertEqual(req.with_user(self.user_emp).employee_leave_balance, 15.0)

    def test_25_sql_constraint_dates(self):
        with self.assertRaises(Exception):
            with self.cr.savepoint():
                self._request(category='paid', date_from=self._monday(14),
                              date_to=self._monday(14) - timedelta(days=3))
