"""AI parts search prototype: type a search in plain language, the local model interprets it, VCdb / PCdb validate it,
and the catalog read replica returns the fitting parts. Run with: streamlit run streamlit_app.py"""
import time

import pandas as pd
import streamlit as st

from app import config
from app.db import connect
from app.llm import parse_query
from app.reference import load_reference
from app.resolve import resolve
from app.search import find_parts

st.set_page_config(page_title='AI Parts Search', layout='wide')


@st.cache_resource
def get_connection():
    return connect()


@st.cache_resource
def get_reference():
    timings = []
    start = time.perf_counter()
    ref = load_reference(get_connection(), timings)
    return ref, time.perf_counter() - start


def apply_patch(patch: dict) -> None:
    overrides = st.session_state['overrides']
    if 'base_vehicle_id' in patch:  # a different vehicle invalidates engine / part choices made for the old one
        overrides.pop('engine_base_ids', None)
        overrides.pop('part_term_ids', None)
        overrides.pop('qualifiers', None)
    overrides.update(patch)


def option_buttons(question, key_prefix: str) -> None:
    columns = st.columns(min(len(question.options), 6) or 1)
    for i, (label, patch) in enumerate(question.options):
        columns[i % len(columns)].button(label, key=f'{key_prefix}-{i}-{label}', on_click=apply_patch, args=(patch,))


st.title('AI Parts Search')
st.caption('Prototype - type a search the way you would say it, e.g. "altenator 2021 dodge ram 5.7"')

with st.sidebar:
    st.subheader('Settings')
    st.write(f'Model: `{config.OLLAMA_MODEL or "not set"}`')
    st.write(f'Database: `{config.DB["host"] or "not set"}`')
    show_details = st.toggle('Show timings and model output', value=True)

conn = get_connection()
conn.ping()
ref, reference_seconds = get_reference()
st.sidebar.write(f'Reference data loaded in {reference_seconds:.1f}s')

with st.form('search'):
    text = st.text_input('Search', placeholder='alternator 2021 ram 1500 5.7')
    submitted = st.form_submit_button('Search', type='primary')

if submitted and text.strip():
    start = time.perf_counter()
    try:
        parsed = parse_query(text)
    except Exception as error:
        st.error(f'The model could not read the search: {error}')
        st.stop()
    st.session_state.update(parsed=parsed, overrides={}, llm_seconds=time.perf_counter() - start)

parsed = st.session_state.get('parsed')
if parsed:
    timings = [('model reads the search', st.session_state['llm_seconds'], 1)]
    res = resolve(parsed, st.session_state['overrides'], ref, conn, timings)

    understood = [res.vehicle_label or (str(res.year) if res.year else ''), res.engine_label if res.vehicle_label else '',
                  res.part_label, res.position, f'brand: {res.brand}' if res.brand else '',
                  f'features: {res.attribute_text}' if res.attribute_text else '']
    st.markdown('**Understood:** ' + ' · '.join(part for part in understood if part))
    for note in res.notes:
        st.caption(f'Corrected: {note}')

    for i, question in enumerate(res.questions):
        st.warning(question.prompt)
        option_buttons(question, f'q{i}')

    if res.ready:
        result = find_parts(conn, res, timings)
        for i, suggestion in enumerate(res.suggestions):
            with st.expander(suggestion.prompt, expanded=suggestion.expanded):
                option_buttons(suggestion, f's{i}')

        # Vehicle qualifiers present in the results (trim, drive type...) - click one to narrow, "Any" to drop it
        selection = result.qualifier_selection
        for qualifier_type, counter in sorted(result.facets.items(), key=lambda f: ref.qualifier_labels.get(f[0], f[0])):
            if len(counter) < 2 and qualifier_type not in selection:
                continue
            label = ref.qualifier_labels.get(qualifier_type, qualifier_type)
            chosen = selection.get(qualifier_type, [])
            st.caption(f'{label}: {", ".join(chosen) if chosen else "any"}')
            values = [value for value, _ in counter.most_common(11)]
            columns = st.columns(len(values) + 1)
            columns[0].button('Any', key=f'f-{qualifier_type}-any', disabled=not chosen, on_click=apply_patch,
                              args=({'qualifiers': {t: v for t, v in selection.items() if t != qualifier_type}},))
            for column, value in zip(columns[1:], values):
                column.button(value, key=f'f-{qualifier_type}-{value}', type='primary' if value in chosen else 'secondary',
                              on_click=apply_patch, args=({'qualifiers': {**selection, qualifier_type: [value]}},))

        for note in result.notes:
            st.info(note)
        clear = st.columns(4)
        if res.brand:
            clear[0].button('Any brand', on_click=apply_patch, args=({'brand': ''},))
        if res.attribute_text:
            clear[1].button('Ignore part features', on_click=apply_patch, args=({'attributes': ''},))
        if result.capped:
            st.info(f'Showing the first {config.MAX_APPS} fitment applications - narrow the search (engine, position) for the rest.')
        st.subheader(f'{len(result.rows)} part(s)')
        if result.rows:
            st.dataframe(pd.DataFrame(result.rows), width='stretch', hide_index=True)
        else:
            st.write('No parts match. Try another engine, or clear the brand, features or fitment filters above.')

    if show_details:
        with st.expander('Timings', expanded=True):
            total = sum(seconds for _, seconds, _ in timings)
            st.dataframe(pd.DataFrame([{'Step': label, 'Seconds': round(seconds, 3), 'Rows': rows_count}
                                       for label, seconds, rows_count in timings]), hide_index=True)
            st.write(f'Total: {total:.2f}s')
        with st.expander('Model output'):
            st.json(parsed)
