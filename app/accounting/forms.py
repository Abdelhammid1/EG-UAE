from flask_wtf import FlaskForm
from wtforms import DateField, SelectField, StringField, TextAreaField
from wtforms.validators import DataRequired, Length, Optional

from app.accounting.models import ACCOUNT_TYPES


class AccountForm(FlaskForm):
    code = StringField("الرمز", validators=[DataRequired(), Length(max=30)])
    name_ar = StringField("الاسم", validators=[DataRequired(), Length(max=160)])
    name_en = StringField("Name (EN)", validators=[Optional(), Length(max=160)])
    type = SelectField("النوع", choices=list(ACCOUNT_TYPES.items()),
                       validators=[DataRequired()])
    parent_id = SelectField("الحساب الأب", coerce=int, validators=[Optional()])
    is_postable = SelectField("قابل للترحيل", choices=[("1", "نعم"), ("0", "لا (تجميعي)")])


class PeriodForm(FlaskForm):
    name = StringField("اسم الفترة", validators=[DataRequired(), Length(max=60)])
    start_date = DateField("من", validators=[DataRequired()])
    end_date = DateField("إلى", validators=[DataRequired()])


class RateForm(FlaskForm):
    from_currency = SelectField("من عملة", validators=[DataRequired()])
    to_currency = SelectField("إلى عملة", validators=[DataRequired()])
    rate = StringField("السعر", validators=[DataRequired()])
