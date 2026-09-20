# Script bindings (SQL ↔ C++)

The world DB binds data to code by **class-name string**: `creature_template.ScriptName =
'boss_kologarn'` is the only link to `struct boss_kologarn`. No code index captures this edge —
query the running DB.

`ObjectMgr::LoadScriptNames()` (`src/server/game/Globals/ObjectMgr.cpp:10449`) is authoritative.
Fourteen tables carry a script name, all in column `ScriptName` except the last:
`achievement_criteria_data` (only `type = 11`), `battleground_template`, `creature`,
`creature_template`, `gameobject`, `gameobject_template`, `item_template`, `areatrigger_scripts`,
`spell_script_names`, `transports`, `game_weather`, `conditions`, `outdoorpvp_template`, and
`instance_template.script`.

## Querying

The live DB has every `data/sql/updates/` file already applied in order, so it resolves
last-write-wins for free; replaying those files statically does not.

```bash
set -a && . ./.env && set +a
docker exec ac-database mysql -uroot -p"$DOCKER_DB_ROOT_PASSWORD" -N -B acore_world -e "<query>"
```

| Question | Query |
|---|---|
| entry → script | `SELECT entry,name,ScriptName FROM creature_template WHERE entry=32930;` |
| script → entries | `SELECT entry,name FROM creature_template WHERE ScriptName='boss_kologarn';` |
| spell → script | `SELECT spell_id,ScriptName FROM spell_script_names WHERE spell_id=62166;` |
| script → spells | `SELECT spell_id FROM spell_script_names WHERE ScriptName='spell_ulduar_stone_grip_aura';` |
| map → instance script | `SELECT map,script FROM instance_template WHERE map=603;` |

Always query both directions: one class usually covers several difficulty variants —
`spell_ulduar_stone_grip_aura` binds 62056 and 63985, its cast-target sibling binds 62166 and 63981.

## SmartAI is not "no script"

`AIName = 'SmartAI'` (9,991 creatures) means the behaviour is `smart_scripts` data (55,518 rows over
14,783 entities) with no C++ at all. An empty `ScriptName` and no C++ file means SmartAI, not
missing:

```sql
SELECT * FROM smart_scripts WHERE entryorguid = 33742 AND source_type = 0;
```

## Offline fallback

With the stack down, `rg 'boss_kologarn' data/sql/base data/sql/updates` resolves **name → entry**,
because a script name is a distinctive string. It fails **entry → name**: a bare `32930` also
matches `broadcast_text` ids, guids, and float substrings such as `1.62166`. Prefer the live query.
