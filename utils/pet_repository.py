
# utils/pet_repository.py
# Pet system repository layer (Supabase/Postgres).
# This module uses the supabase client defined in utils.database.

from __future__ import annotations
from typing import Optional, Dict, Any, List, Sequence
from datetime import datetime, timezone
import math

from utils.database import supabase  # reuse existing client

# ---------- Helpers ----------

def _utc_now() -> datetime:
    return datetime.now(timezone.utc)

def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()

def _first(data: Any) -> Optional[dict]:
    if not data:
        return None
    if isinstance(data, dict):
        return data
    if isinstance(data, list) and data:
        return data[0]
    return None

# ---------- Inventory & Items ----------

def get_user_inventory_eggs(owner_id: int) -> List[Dict[str, Any]]:
    """Return list of eggs in inventory (item_name, quantity), filtered by items.category='egg'."""
    eggs_resp = supabase.table("items").select("name,id_key,category").eq("category", "egg").execute()
    egg_names = {r["name"] for r in eggs_resp.data or []}
    if not egg_names:
        return []

    inv_resp = supabase.table("inventories").select("item_name, quantity").eq("user_id", owner_id).execute()
    rows = inv_resp.data or []
    out = []
    for r in rows:
        if r["item_name"] in egg_names and int(r["quantity"]) > 0:
            out.append({"item_name": r["item_name"], "quantity": int(r["quantity"])})
    return out

def decrement_inventory_item(owner_id: int, item_name: str, qty: int) -> bool:
    """Optimistic inventory decrement. Returns True on success, False otherwise."""
    inv = supabase.table("inventories").select("quantity").eq("user_id", owner_id).eq("item_name", item_name).single().execute()
    row = inv.data
    if not row:
        return False
    cur = int(row["quantity"])
    if cur < qty:
        return False
    new_q = cur - qty
    if new_q <= 0:
        supabase.table("inventories").delete().eq("user_id", owner_id).eq("item_name", item_name).execute()
    else:
        supabase.table("inventories").update({"quantity": new_q}).eq("user_id", owner_id).eq("item_name", item_name).execute()
    return True

# ---------- Species / Attributes ----------

def get_species_by_id_key(id_key: str) -> Optional[Dict[str, Any]]:
    """Map items.id_key -> dragon_species row (species_key == id_key)."""
    rsp = supabase.table("dragon_species").select("*").eq("species_key", id_key).single().execute()
    return rsp.data

def get_species(species_key: str) -> Optional[Dict[str, Any]]:
    rsp = supabase.table("dragon_species").select("*").eq("species_key", species_key).single().execute()
    return rsp.data

# ---------- Pet Incubation ----------

def get_active_incubation(owner_id: int) -> Optional[Dict[str, Any]]:
    rsp = supabase.table("pet_incubations").select("*").eq("owner_id", owner_id).eq("status", "incubating").order("started_at", desc=True).limit(1).execute()
    data = rsp.data or []
    return data[0] if data else None

def create_pet_incubation(owner_id: int, egg_item_name: str, hatch_at_utc: datetime) -> Dict[str, Any]:
    row = {
        "owner_id": owner_id,
        "egg_item_name": egg_item_name,
        "hatch_at": _iso(hatch_at_utc),
        "status": "incubating"
    }
    ins = supabase.table("pet_incubations").insert(row).select("*").single().execute()
    return ins.data

def cancel_pet_incubation(incubation_id: int) -> None:
    supabase.table("pet_incubations").update({"status": "canceled"}).eq("id", incubation_id).execute()

def list_due_incubations(limit: int = 50) -> List[Dict[str, Any]]:
    """Return incubations where hatch_at <= now() and status='incubating'."""
    now_iso = _iso(_utc_now())
    rsp = supabase.table("pet_incubations").select("*").eq("status", "incubating").lte("hatch_at", now_iso).limit(limit).execute()
    return rsp.data or []

# ---------- Pets ----------

def create_pet_from_incubation(inc: Dict[str, Any]) -> Dict[str, Any]:
    """
    Convert an incubation row to a pet. Uses items.id_key of the egg to find species_key,
    then reads base stats from dragon_species. Sets stage to 'hatch'.
    """
    owner_id = inc["owner_id"]
    egg_name = inc["egg_item_name"]

    # Find species via items.id_key
    item = supabase.table("items").select("name, id_key").eq("name", egg_name).single().execute().data
    species_key = (item or {}).get("id_key") or egg_name  # fallback to egg name
    species = get_species(species_key)
    if not species:
        # Fallback generic stats
        species = {
            "species_key": species_key,
            "display_name": species_key,
            "attribute_key": "fire",
            "base_hp": 50, "base_atk": 10, "base_def": 10, "base_spd": 10,
            "image_url": None
        }

    row = {
        "owner_id": owner_id,
        "species_key": species["species_key"],
        "attribute_key": species["attribute_key"],
        "stage_key": "hatch",
        "level": 1,
        "exp": 0,
        "image_url": species.get("image_url"),
        "hp": int(species.get("base_hp", 50)),
        "atk": int(species.get("base_atk", 10)),
        "def": int(species.get("base_def", 10)),
        "spd": int(species.get("base_spd", 10)),
        "affinity": 0,
        "hunger": 0
    }
    pet = supabase.table("pets").insert(row).select("*").single().execute().data
    # mark incubation as hatched
    supabase.table("pet_incubations").update({"status": "hatched"}).eq("id", inc["id"]).execute()
    return pet

def get_active_pet(owner_id: int) -> Optional[Dict[str, Any]]:
    rsp = supabase.table("pets").select("*").eq("owner_id", owner_id).limit(1).execute()
    data = rsp.data or []
    return data[0] if data else None

def update_pet_stats(pet_id: int, **stats) -> None:
    supabase.table("pets").update(stats).eq("id", pet_id).execute()

# ---------- Panel message tracking ----------

def get_pet_panel_message_info(owner_id: int) -> Optional[Dict[str, Any]]:
    rsp = supabase.table("thread_message_info").select("*").eq("owner_id", owner_id).eq("panel", "pet_panel").single().execute()
    return rsp.data

def save_pet_panel_message_info(owner_id: int, thread_id: int, message_id: int) -> None:
    existing = supabase.table("thread_message_info").select("*").eq("owner_id", owner_id).eq("panel", "pet_panel").single().execute()
    row = {"owner_id": owner_id, "panel": "pet_panel", "thread_id": thread_id, "message_id": message_id}
    if existing.data:
        supabase.table("thread_message_info").update(row).eq("owner_id", owner_id).eq("panel", "pet_panel").execute()
    else:
        supabase.table("thread_message_info").insert(row).execute()
