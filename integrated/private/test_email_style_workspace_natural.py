from unittest.mock import patch
import pytest
import cr_email_style as E,cr_workspace_create as W

def test_fixed_html_escaped_and_signature_optional():
 html=E.render('<script>alert(1)</script>\nHi\n\nGoodbye')
 assert '<script>' not in html and '&lt;script&gt;' in html and 'Sent by Crayon AI' not in html
 assert 'Sent by Crayon AI' in E.render('Hello',True)
 assert 'src=' not in html

def test_natural_doc_composes_then_validates():
 with patch('cr_llm.ask_json',return_value={'title':'Photosynthesis','content':'Plants use light.'}):
  assert W.natural_fields('Make a doc on photosynthesis')=={'kind':'doc','title':'Photosynthesis','content':'Plants use light.'}
def test_missing_expense_source_asks():
 with patch('cr_llm.ask_json',return_value={'question':'Please paste your expenses.'}):
  with pytest.raises(ValueError,match='expenses'):W.natural_fields('Make a sheet of my expenses')
def test_invalid_composed_rows_rejected():
 with patch('cr_llm.ask_json',return_value={'title':'Expenses','content':'invented'}):
  with pytest.raises(ValueError):W.natural_fields('Make a sheet with groceries 200')
