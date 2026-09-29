from flask_wtf import FlaskForm
from wtforms import (
    DateField,
    IntegerField,
    SelectField,
    StringField,
    TextAreaField,
)
from wtforms.validators import DataRequired, Length, Optional

from app.treasury.models import TREASURY_TYPES


class TreasuryForm(FlaskForm):
    name_ar = StringField("اسم الخزينة", validators=[DataRequired(), Length(max=120)])
    branch_id = SelectField("الفرع", coerce=int, validators=[DataRequired()])
    currency_code = SelectField("العملة", validators=[DataRequired()])
    type = SelectField("النوع", choices=list(TREASURY_TYPES.items()))
    opening_balance = StringField("الرصيد الافتتاحي", default="0")


class BankForm(FlaskForm):
    name_ar = StringField("اسم الحساب", validators=[DataRequired(), Length(max=120)])
    bank_name = StringField("البنك", validators=[Optional(), Length(max=120)])
    account_number = StringField("رقم الحساب", validators=[Optional(), Length(max=60)])
    branch_id = SelectField("الفرع", coerce=int, validators=[DataRequired()])
    currency_code = SelectField("العملة", validators=[DataRequired()])
    opening_balance = StringField("الرصيد الافتتاحي", default="0")
