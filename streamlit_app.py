                        st.metric("Visible numeric total", f"£{numeric_values.sum():,.0f}")

            st.download_button(
                f"Download {sheet_name} as CSV",
                data=edited_df.to_csv(index=False).encode("utf-8"),
                file_name=f"{sheet_name.lower()}-calculator.csv",
                mime="text/csv",
                key=f"download_{sheet_name}_csv",
            )

    if edited_sheets:
        workbook_bytes = build_workbook_download(edited_sheets)
        st.download_button(
            "Download edited workbook",
            data=workbook_bytes,
            file_name="deal-calculator-edited.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    if template_bytes:
        st.download_button(
            "Download original template",
            data=template_bytes,
            file_name=DEAL_TEMPLATE_FILE,
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )


# ============================
# ANALYSE PAGE
# ============================
if page == "Analyse Deal":
    land_data = load_data()

    if land_data is None:
        st.warning("Comparable sales data could not be loaded. The deal analysis page will run without comps.")

    url = st.text_input("Rightmove URL")

    st.subheader("Condition")
    current = st.selectbox("Current Condition", ["Poor", "Fair", "Good", "Very Good"])
    target = st.selectbox("Target Condition", ["Good", "Very Good", "Luxury"])

    st.subheader("Refurb")
    kitchen = st.number_input("Kitchen", min_value=0, value=5000, step=500)
    bathroom = st.number_input("Bathroom", min_value=0, value=4000, step=500)
    electrics = st.number_input("Electrics", min_value=0, value=3000, step=500)
    plumbing = st.number_input("Plumbing", min_value=0, value=3000, step=500)
    plastering = st.number_input("Plastering", min_value=0, value=2500, step=500)
    flooring = st.number_input("Flooring", min_value=0, value=2000, step=500)
    paint = st.number_input("Paint", min_value=0, value=1500, step=500)

    contingency = st.slider("Contingency %", 0, 25, 10)

    if st.button("Analyse"):
        if not url:
            st.error("Add a Rightmove URL before running the analysis.")
        else:
            try:
                html = get_html(url)
                data = extract(html)

                price = parse_price(data["price"])
                sqm = estimate_sqm(data["bedrooms"])
                street, postcode = extract_location(data["name"])
                comps = find_comps(postcode, land_data)
                multiplier = condition_multiplier(current, target)

                refurb = refurb_engine(
                    {
                        "kitchen": kitchen,
                        "bathroom": bathroom,
                        "electrics": electrics,
                        "plumbing": plumbing,
                        "plastering": plastering,
                        "flooring": flooring,
                        "paint": paint,
                    },
                    contingency,
                    multiplier,
                )

                result = analyse(price, sqm, 2400, refurb["total"])

                st.session_state.data = data
                st.session_state.result = result
                st.session_state.comps = comps
                st.session_state.refurb = refurb
                st.session_state.refurb_total = refurb["total"]
                st.session_state.current_condition = current
                st.session_state.target_condition = target
                st.session_state.analysis_done = True

                st.success("Analysis complete")
                st.metric("ROI", f"{result['roi']}%")
                st.metric("Profit", f"£{result['profit']:,}")

            except Exception as exc:
                st.error(f"Analysis failed: {exc}")

    if st.session_state.analysis_done:
        st.divider()
        st.subheader("Deal Dashboard")

        data = st.session_state.data
        result = st.session_state.result
        refurb = st.session_state.refurb
        comps = st.session_state.comps

        st.subheader("Property Under Review")
        st.markdown(f"**{data['name']}**")

        st.subheader("Condition")
        st.write(f"Current: {st.session_state.current_condition}")
        st.write(f"Target: {st.session_state.target_condition}")

        st.subheader("Refurbishment")
        st.write(refurb)

        st.subheader("Comparables")
        if comps is not None and len(comps) > 0:
            for _, row in comps.iterrows():
                link = zoopla_link(row["street"], row["postcode"])
                st.markdown(
                    f"""
                    **{row['street']}**  
                    £{int(row['price']):,}  
                    [View Property]({link})
                    ---
                    """
                )
        else:
            st.write("No comparables found for this property yet.")

        st.subheader("Headline Numbers")
        col1, col2, col3 = st.columns(3)
        col1.metric("GDV", f"£{result['gdv']:,}")
        col2.metric("Profit", f"£{result['profit']:,}")
        col3.metric("ROI", f"{result['roi']}%")

elif page == "Deal Calculator":
    render_calculator_page()

else:
    st.subheader(page)
    st.info("This section is ready for the next feature once you want to expand the app further.")
