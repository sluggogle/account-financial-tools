# Copyright 2009-2018 Noviat
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# List of move's fields that can't be modified if move is linked
# with a depreciation line
FIELDS_AFFECTS_ASSET_MOVE = {"journal_id", "date"}
# List of move line's fields that can't be modified if move is linked
# with a depreciation line
FIELDS_AFFECTS_ASSET_MOVE_LINE = {
    "credit",
    "debit",
    "account_id",
    "journal_id",
    "date",
    "asset_profile_id",
    "asset_id",
}


class AccountMove(models.Model):
    _inherit = "account.move"

    def unlink(self):
        # for move in self:
        deprs = self.env["account.asset.line"].search(
            [("move_id", "in", self.ids), ("type", "in", ["depreciate", "remove"])]
        )
        if deprs and not self.env.context.get("unlink_from_asset"):
            raise UserError(
                _(
                    "You are not allowed to remove an accounting entry "
                    "linked to an asset."
                    "\nYou should remove such entries from the asset."
                )
            )
        # trigger store function
        deprs.write({"move_id": False})
        return super().unlink()

    def write(self, vals):
        if set(vals).intersection(FIELDS_AFFECTS_ASSET_MOVE):
            deprs = self.env["account.asset.line"].search(
                [("move_id", "in", self.ids), ("type", "=", "depreciate")]
            )
            if deprs:
                raise UserError(
                    _(
                        "You cannot change an accounting entry "
                        "linked to an asset depreciation line."
                    )
                )
        return super().write(vals)

    def post(self):
        super().post()
        for move in self:
            for aml in move.line_ids.filtered("asset_profile_id"):
                depreciation_base = aml.debit or -aml.credit
                vals = {
                    "name": aml.name,
                    "code": move.name,
                    "profile_id": aml.asset_profile_id.id,
                    "purchase_value": depreciation_base,
                    "partner_id": aml.partner_id.id,
                    "date_start": move.date,
                    "account_analytic_id": aml.analytic_account_id.id,
                }
                if self.env.context.get("company_id"):
                    vals["company_id"] = self.env.context["company_id"]
                asset = (
                    self.env["account.asset"]
                    .with_context(create_asset_from_move_line=True, move_id=move.id)
                    .create(vals)
                )
                aml.with_context(allow_asset=True).asset_id = asset.id
            refs = [
                "<a href=# data-oe-model=account.asset data-oe-id=%s>%s</a>"
                % tuple(name_get)
                for name_get in move.line_ids.filtered(
                    "asset_profile_id"
                ).asset_id.name_get()
            ]
            message = _("This invoice created the asset(s): %s") % ", ".join(refs)
            move.message_post(body=message)

    def button_draft(self):
        invoices = self.filtered(lambda r: not r.is_sale_document())
        if invoices:
            invoices.line_ids.asset_id.unlink()
        super().button_draft()

    def _reverse_move_vals(self, default_values, cancel=True):
        move_vals = super()._reverse_move_vals(default_values, cancel)
        if move_vals["type"] not in ("out_invoice", "out_refund"):
            for line_command in move_vals.get("line_ids", []):
                line_vals = line_command[2]  # (0, 0, {...})
                asset = self.env["account.asset"].browse(line_vals["asset_id"])
                asset.unlink()
                line_vals.update(asset_profile_id=False, asset_id=False)
        return move_vals


class AccountMoveLine(models.Model):
    _inherit = "account.move.line"

    asset_profile_id = fields.Many2one(
        comodel_name="account.asset.profile", string="Asset Profile"
    )
    asset_id = fields.Many2one(
        comodel_name="account.asset", string="Asset", ondelete="restrict"
    )

    @api.onchange("account_id")
    def _onchange_account_id(self):
        self.asset_profile_id = self.account_id.asset_profile_id
        super()._onchange_account_id()

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            move = self.env["account.move"].browse(vals.get("move_id"))
            if not move.is_sale_document():
                if vals.get("asset_id") and not self.env.context.get("allow_asset"):
                    raise UserError(
                        _(
                            "You are not allowed to link "
                            "an accounting entry to an asset."
                            "\nYou should generate such entries from the asset."
                        )
                    )
        records = super().create(vals_list)
        for record in records:
            record._expand_asset_line()
        return records

    def write(self, vals):
        if set(vals).intersection(FIELDS_AFFECTS_ASSET_MOVE_LINE) and not (
            self.env.context.get("allow_asset_removal")
            and list(vals.keys()) == ["asset_id"]
        ):
            # Check if at least one asset is linked to a move
            linked_asset = False
            for move_line in self.filtered(lambda r: not r.move_id.is_sale_document()):
                linked_asset = move_line.asset_id
                if linked_asset:
                    raise UserError(
                        _(
                            "You cannot change an accounting item "
                            "linked to an asset depreciation line."
                        )
                    )

        if (
            self.filtered(lambda r: not r.move_id.is_sale_document())
            and vals.get("asset_id")
            and not self.env.context.get("allow_asset")
        ):
            raise UserError(
                _(
                    "You are not allowed to link "
                    "an accounting entry to an asset."
                    "\nYou should generate such entries from the asset."
                )
            )
        super().write(vals)
        if "quantity" in vals or "asset_profile_id" in vals:
            for record in self:
                record._expand_asset_line()
        return True

    def _expand_asset_line(self):
        self.ensure_one()
        if self.asset_profile_id and self.quantity > 1.0:
            profile = self.asset_profile_id
            if profile.asset_product_item:
                aml = self.with_context(check_move_validity=False)
                qty = self.quantity
                name = self.name
                aml.write({"quantity": 1, "name": "{} {}".format(name, 1)})
                aml._onchange_price_subtotal()
                for i in range(1, int(qty)):
                    aml.copy({"name": "{} {}".format(name, i + 1)})
                aml.move_id._onchange_invoice_line_ids()
