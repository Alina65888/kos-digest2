import copy
import json
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from src.appointment_wishes import split_message, wish_problem, RESERVE_WISHES
from src.appointments_pipeline import build_appointments_draft, regenerate_single_person


def person(name='Иванов Иван', order=0, **kwargs):
    return dict(name=name, section='new_challenge', new_position='Инженер', notes='', order=order, **kwargs)


def test_duplicates_and_cliches_are_rejected_but_distinct_wishes_pass():
    assert wish_problem('Сердечно поздравляем с назначением на новую должность!', [])
    assert wish_problem('Пусть новая работа приносит только хорошие впечатления.', [RESERVE_WISHES[0]])
    assert wish_problem(RESERVE_WISHES[1].upper(), [RESERVE_WISHES[1]])
    assert wish_problem('Удачи в новой роли! Желаем побольше интересных проектов.', [RESERVE_WISHES[6]])
    assert not wish_problem(RESERVE_WISHES[1], [RESERVE_WISHES[0]])


def test_wishes_follow_display_order_and_retry_with_previous_context():
    people = [person('Петров Петр', 2), person('Иванов Иван', 1)]
    answers = iter([
        {'transition': 'Иван перешел на новую должность.', 'wish': RESERVE_WISHES[0]},
        {'transition': '', 'wish': RESERVE_WISHES[0]},
        {'transition': '', 'wish': RESERVE_WISHES[1]},
    ])
    payloads = []
    def generate(system, payload, **kwargs):
        payloads.append(json.loads(payload))
        return next(answers)
    with patch('src.appointments_pipeline.llm_json', side_effect=generate):
        draft = build_appointments_draft(people)
    assert [p['person']['name'] for p in payloads] == ['Иванов Иван', 'Петров Петр', 'Петров Петр']
    assert payloads[1]['editorial_context']['used_wishes'] == [RESERVE_WISHES[0]]
    assert payloads[2]['editorial_context']['revision_reason']
    assert payloads[2]['editorial_context']['previous_wish'] == RESERVE_WISHES[0]
    assert not draft['warnings']
    assert people[0]['new_position'] == 'Инженер'
    assert split_message(people[0]['message'])[1] == RESERVE_WISHES[1]


def test_persistent_bad_output_uses_distinct_reserve_and_warns():
    people = [person('Иванов Иван'), person('Петров Петр')]
    with patch('src.appointments_pipeline.llm_json', return_value={
        'transition': '', 'wish': 'Сердечно поздравляем с назначением на новую должность!',
    }) as generate:
        draft = build_appointments_draft(people)
    assert generate.call_count == 4
    assert people[0]['message'] != people[1]['message']
    assert len(draft['warnings']) == 2
    assert all('резервное' in warning for warning in draft['warnings'])


def test_regeneration_keeps_approved_fact_and_avoids_current_and_other_wishes():
    target = person(message='Согласованная информация о переходе.<br><br>' + RESERVE_WISHES[0])
    other = person('Петров Петр', message=RESERVE_WISHES[1])
    before = copy.deepcopy(other)
    draft = {'sections': [{'key': 'new_challenge', 'people': [target, other]}]}
    with patch('src.appointments_pipeline.llm_json', return_value={
        'transition': 'Неодобренное изменение факта.', 'wish': RESERVE_WISHES[2],
    }) as generate:
        regenerate_single_person(draft, 'new_challenge', 0)
    context = json.loads(generate.call_args.args[1])['editorial_context']
    assert set(context['used_wishes']) == {RESERVE_WISHES[0], RESERVE_WISHES[1]}
    assert split_message(target['message']) == ('Согласованная информация о переходе.', RESERVE_WISHES[2])
    assert other == before


def test_api_failure_remains_visible_without_fabricated_facts():
    with patch('src.appointments_pipeline.llm_json', side_effect=RuntimeError('API unavailable')):
        draft = build_appointments_draft([person()])
    assert 'API unavailable' in draft['warnings'][0]
    target = next(s for s in draft['sections'] if s['key'] == 'new_challenge')['people'][0]
    assert split_message(target['message'])[0] == ''


def test_biographies_and_empty_departures_keep_existing_contract():
    people = [dict(person(), section='key'), dict(person('Петров Петр'), section='departed')]
    with patch('src.appointments_pipeline.llm_json', return_value={'education': 'Образование.', 'career': 'Карьера.'}) as generate:
        build_appointments_draft(people)
    assert generate.call_count == 1
    assert people[0]['career'] == 'Карьера.'
    assert people[1]['message'] == ''


def test_regeneration_refreshes_textarea_and_preserves_latest_editor_fact():
    app = AppTest.from_file(str(Path(__file__).parents[1] / 'app.py'), default_timeout=20)
    app.session_state['digest_mode'] = 'appointments'
    app.session_state['appt_draft'] = {
        'sections': [{'key': 'new_challenge', 'title': 'Новый вызов', 'people': [
            person(message='Первоначальный факт.<br><br>' + RESERVE_WISHES[0])]}],
        'warnings': [],
    }
    app.run()
    assert not app.exception
    app.text_area(key='appt_msg_0_0').set_value('Правка редактора.<br><br>' + RESERVE_WISHES[0]).run()
    with patch('src.appointments_pipeline.llm_json', return_value={'transition': 'Неверный факт.', 'wish': RESERVE_WISHES[1]}):
        app.button(key='appt_rg_0_0').click().run()
    assert not app.exception
    assert app.text_area(key='appt_msg_0_0').value == 'Правка редактора.<br><br>' + RESERVE_WISHES[1]
    app.button(key='appt_sv_0_0').click().run()
    assert app.session_state['appt_draft']['sections'][0]['people'][0]['message'].endswith(RESERVE_WISHES[1])
