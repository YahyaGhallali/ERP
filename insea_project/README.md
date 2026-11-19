# INSEA – Absences & Congés (Odoo 18 module)

Mini-projet Systèmes Intégrés 2026-2027. Custom Odoo module `insea_hr_leave_custom`.

Tested on **Odoo 18 Community**: installs cleanly, 25/25 automated tests pass,
all screens checked in the browser with each demo profile.

---

## 1. Run it (Docker)

```
insea_project/
├── docker-compose.yml
└── addons/
    └── insea_hr_leave_custom/     <- the module folder from this zip
```

1. Install Docker Desktop.
2. In `insea_project/`, run: `docker compose up`
3. Open http://localhost:8069 → create a database, **tick "Demo data"**.
4. Apps → remove the "Apps" filter → search **INSEA** → Activate.
5. Log out and log in with a demo account below.

After changing code:
- Python (`.py`) → `docker compose restart odoo`, then Apps → module → **Upgrade**
- XML views / security → Apps → module → **Upgrade**
- Turn on Developer Mode (Settings → bottom) to see technical menus.

### Demo accounts (login / password)

| Login | Password | Person | Role |
|---|---|---|---|
| `rh` | `rh` | Salma Benali | Responsable RH (final validation, sees all) |
| `manager` | `manager` | Karim Alaoui | Manager N+1 of the team |
| `youssef` | `youssef` | Youssef Amrani | Employee (21 days earned) |
| `imane` | `imane` | Imane Tazi | Employee, hired 2 months ago (3 days) |
| `omar` | `omar` | Omar Idrissi | Employee |
| `admin` | `admin` | Administrator | System admin (also HR) |

Demo requests cover every state: draft, submitted, waiting HR, approved, refused,
plus a medical authorization without proof (to show the blocking rule).

### Run the automated tests

```
docker compose exec odoo odoo -d test_db -i insea_hr_leave_custom \
  --test-enable --test-tags /insea_hr_leave_custom --stop-after-init \
  --db_host db -r odoo -w odoo --http-port 8070
```
Expected: `0 failed, 0 error(s) of 25 tests`.
(An "ERROR … check_dates_order" line in the log is expected: test 25 triggers the SQL constraint on purpose.)

---

## 2. What is where (layer by layer)

| Layer | File(s) | Content |
|---|---|---|
| 1. Skeleton | `__manifest__.py`, `__init__.py` | Module declaration, dependencies (`hr`, `mail`), load order |
| 2. Data model | `models/hr_employee.py` | **`_inherit = 'hr.employee'`**: hire date, carry-over, computed balance (accrued / taken / pending / balance) |
| | `models/absence_request.py` §2-3 | **New model `_name = 'insea.absence.request'`**: fields + computed fields (`@api.depends`) |
| 3. Workflow | `models/absence_request.py` §6 | State machine: `action_submit`, `action_manager_approve`, `action_hr_approve`, refusal, reset |
| | `wizard/refuse_wizard.py` | `TransientModel` that forces a refusal reason |
| 4. Business rules | `models/absence_request.py` §5 | `@api.constrains` + `_sql_constraints` + checks at submit / approval |
| 5. Security | `security/security.xml` | 3 groups (Employee < Manager < HR) + record rules |
| | `security/ir.model.access.csv` | Model access rights |
| 6. Interface | `views/absence_request_views.xml` | Form (statusbar), list, search filters, calendar, graph, pivot, actions |
| | `views/hr_employee_views.xml` | Inherited employee form (balance tab + smart button), balances list |
| | `views/menus.xml` | App menu |
| 7. Data & tests | `data/ir_sequence_data.xml` | Reference numbers ABS/2026/00001 |
| | `demo/demo_data.xml` | Representative dataset for the defense |
| | `tests/test_absence_request.py` | 25 automated tests (one per rule) |

## 3. Business rules → where they are enforced

| Rule (cahier des charges) | Implementation |
|---|---|
| Authorization ≤ 4 h | `_check_authorization_duration` (constrains) |
| Max 3 authorizations **or** 8 h / month | `_check_authorization_monthly_cap` (constrains) |
| Reason mandatory | `_check_required_fields` + `required=` in the view |
| Authorization doesn't touch the balance | `duration_days = 0`, `impacts_balance = False` |
| 1.5 days accrued per month worked | `hr.employee._compute_leave_balance` |
| Balance ≥ requested duration | `_check_balance` at submission **and** HR approval (pending requests are reserved) |
| No overlap with approved / pending requests | `_check_overlap` (constrains on unified start/end datetimes) |
| Paid leave > 3 days → 7 calendar days notice | `_check_notice_period` at submission |
| Proof required → final validation blocked | `_check_attachment_before_final` |
| Family event durations (4 / 3 / 2-3 days) | `_check_family_event_duration` + onchange that pre-fills the end date |
| Not editable after submission | `write()` override (locked fields, workflow fields only via buttons) |
| Refusal needs a reason + notification | wizard + `_do_refuse` + `message_post` to the employee |
| Authorization: manager approval is final | `requires_hr_validation = False` → state `approved` directly |
| Paid / exceptional / sick > 1 day → HR | `requires_hr_validation` computed field |
| Employee sees own / manager sees team / HR sees all | 3 `ir.rule` records |

## 4. Design choices to justify in the report

1. **New model instead of extending `hr.leave`.** `hr.leave` has its own state machine
   (confirm / validate1 / validate) and allocation system. Our workflow and rules are
   different (authorizations in hours with monthly caps, custom balance), so a dedicated
   model is cleaner. Standard classes are still reused: `hr.employee` (extended with `_inherit`),
   `res.users`, `hr.department`, `ir.attachment`, `mail.thread` (chatter, notifications),
   `mail.activity.mixin` (to-do for the manager), `resource.calendar.leaves` (public holidays).
2. **One model with a `category` field** for the 4 request types, so there is one workflow,
   one overlap check, one calendar.
3. **"Jours ouvrés"**: Monday-Friday, minus public holidays configured in
   *Configuration → Jours fériés*.
4. **Sick leave of exactly 1 day**: validated by the manager only (spec says HR for > 1 day),
   but the certificate is still mandatory before that validation.
5. **Balance is computed, not stored**: always up to date (it depends on today's date),
   no scheduled job needed. Pending paid leaves are reserved so an employee can't
   submit two requests that together exceed the balance.
6. **Self-approval is forbidden**: a manager's own request goes to his own N+1.

## 5. Using another Odoo version?

- **Odoo 17**: replace `<list` / `</list>` with `<tree` / `</tree>` in the XML views, and
  `<chatter/>` with
  `<div class="oe_chatter"><field name="message_follower_ids"/><field name="activity_ids"/><field name="message_ids"/></div>`.
  Change `version` in the manifest to `17.0.1.0.0`.
- **Odoo 19+**: `groups_id` → `group_ids`, `users` → `user_ids` on groups, and
  `_sql_constraints` → `models.Constraint`. Ask for help if you need this port.
