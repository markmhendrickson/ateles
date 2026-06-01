import pandas as pd

# Load data
holdings = pd.read_parquet("data/holdings/holdings.parquet")
balances = pd.read_parquet("data/balances/balances.parquet")
properties = pd.read_parquet("data/properties/properties.parquet")
liabilities = pd.read_parquet("data/liabilities/liabilities.parquet")

# Get most recent data with non-zero values
# Check all snapshot dates and find the most recent with actual values
holdings_by_date = holdings.groupby("snapshot_date")["current_value_usd"].sum()
latest_holdings_date = holdings_by_date[holdings_by_date > 0].index.max()
if pd.isna(latest_holdings_date):
    # Fallback to most recent date if all are zero
    latest_holdings_date = holdings["snapshot_date"].max()

latest_balances_date = balances["snapshot_date"].max()

latest_holdings = holdings[holdings["snapshot_date"] == latest_holdings_date]
latest_balances = balances[balances["snapshot_date"] == latest_balances_date]

print(f"=== HOLDINGS (Snapshot: {latest_holdings_date}) ===")
holdings_value = latest_holdings["current_value_usd"].sum()

# Real estate from holdings
real_estate_holdings = latest_holdings[
    latest_holdings["asset_name"].str.contains(
        "Passatge|San Vicente", case=False, na=False
    )
]
real_estate_value = real_estate_holdings["current_value_usd"].sum()

# Other holdings (excluding real estate)
other_holdings = latest_holdings[
    ~latest_holdings["asset_name"].str.contains(
        "Passatge|San Vicente", case=False, na=False
    )
]
other_holdings_value = other_holdings["current_value_usd"].sum()

print(f"Total holdings value: ${holdings_value:,.2f}")
print(f"  Real Estate: ${real_estate_value:,.2f}")
print(f"  Other Holdings: ${other_holdings_value:,.2f}")
print("\nBy asset type:")
asset_type_summary = (
    latest_holdings.groupby("asset_type")["current_value_usd"]
    .sum()
    .sort_values(ascending=False)
)
for asset_type, value in asset_type_summary.items():
    print(f"  {asset_type}: ${value:,.2f}")

print(f"\n=== BALANCES (Snapshot: {latest_balances_date}) ===")
balances_value = latest_balances["balance_usd"].sum()
print(f"Total balances: ${balances_value:,.2f}")

print("\n=== LIABILITIES ===")
total_liabilities = abs(liabilities["amount_usd"].sum())
print(f"Total liabilities: ${total_liabilities:,.2f}")
print("\nBreakdown:")
for idx, row in liabilities.iterrows():
    print("  {}: ${:,.2f}".format(row["name"], abs(row["amount_usd"])))

print("\n=== NET WORTH CALCULATION ===")
total_assets = holdings_value + balances_value
net_worth_usd = total_assets - total_liabilities
net_worth_eur = net_worth_usd / 1.08

print(f"Holdings value: ${holdings_value:,.2f}")
print(f"Balances: ${balances_value:,.2f}")
print(f"\nTotal Assets: ${total_assets:,.2f}")
print(f"Total Liabilities: ${total_liabilities:,.2f}")
print(f"\nNET WORTH: ${net_worth_usd:,.2f}")
print(f"NET WORTH (EUR): €{net_worth_eur:,.2f}")
