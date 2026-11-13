# Dataset data dictionary

一行 = 一局 × 一分钟。`side=blue` 时特征为蓝方减红方；`side=red` 时所有 `*_diff` 取反。

## 标识与标签

| 字段 | 说明 |
|---|---|
| gameId | 对局 ID |
| minute | 第几分钟（1..N） |
| patch | 版本，如 16.22 |
| queue_id | 420 单双排 / 440 灵活排位 |
| side | blue / red |
| win | 该行视角是否获胜（0/1） |
| blue_win | 蓝方是否获胜（0/1），便于核对 |
| gold_diff_plus5 | 5 分钟后的经济差（该行视角），最后一帧不足 5 分钟时为空 |

## 队伍级特征（该行视角 - 对方）

| 字段 | 说明 |
|---|---|
| gold_diff | 总金币差（totalGold 之和） |
| current_gold_diff | 当前可用金币差 |
| xp_diff | 总经验差 |
| level_diff | 总等级差 |
| cs_diff | 总补刀差（含野怪） |
| kills_diff | 累计击杀差 |
| deaths_diff | 累计死亡差 |
| assists_diff | 累计助攻差 |
| tower_diff | 累计推塔差 |
| inhibitor_diff | 累计水晶差 |
| dragon_diff | 累计小龙差 |
| elder_dragon_diff | 累计远古龙差 |
| baron_diff | 累计大龙差 |
| herald_diff | 累计先锋差 |
| grub_diff | 累计巢虫差 |
| atakhan_diff | 累计厄塔汗差 |
| plate_diff | 累计镀层差 |
| wards_placed_diff | 累计插眼差 |
| wards_killed_diff | 累计排眼差 |
| gold_diff_slope_5m | 最近 5 分钟经济差斜率 |
| xp_diff_slope_5m | 最近 5 分钟经验差斜率 |
| kills_last_5m | 最近 5 分钟击杀差 |
| towers_last_5m | 最近 5 分钟推塔差 |
| objectives_last_5m | 最近 5 分钟史诗野怪差 |
| kills_last_min | 本分钟击杀差 |
| objectives_last_min | 本分钟史诗野怪差 |
| kill_lane_top_diff | 击杀发生在上路的次数差 |
| kill_lane_mid_diff | 击杀发生中路的次数差 |
| kill_lane_bot_diff | 击杀发生下路的次数差 |
| kill_lane_jungle_diff | 击杀发生在野区的次数差 |
| first_blood | 一血方（+1 蓝 / -1 红 / 0 未知） |
| jungler_zone_top_diff | 双方打野当前处于上区的差 |
| jungler_zone_mid_diff | 双方打野当前处于中区的差 |
| jungler_zone_bot_diff | 双方打野当前处于下区的差 |
| jungler_near_top_diff | 双方打野靠近上路的差 |
| jungler_near_mid_diff | 双方打野靠近中路的差 |
| jungler_near_bot_diff | 双方打野靠近下路的差 |
| jungler_gank_top_diff | 双方打野参与上路击杀的累计差 |
| jungler_gank_mid_diff | 双方打野参与中路击杀的累计差 |
| jungler_gank_bot_diff | 双方打野参与下路击杀的累计差 |
| gold_diff_TOP | 分位置差值 |
| xp_diff_TOP | 分位置差值 |
| level_diff_TOP | 分位置差值 |
| cs_diff_TOP | 分位置差值 |
| kills_diff_TOP | 分位置差值 |
| deaths_diff_TOP | 分位置差值 |
| assists_diff_TOP | 分位置差值 |
| gold_diff_JUNGLE | 分位置差值 |
| xp_diff_JUNGLE | 分位置差值 |
| level_diff_JUNGLE | 分位置差值 |
| cs_diff_JUNGLE | 分位置差值 |
| kills_diff_JUNGLE | 分位置差值 |
| deaths_diff_JUNGLE | 分位置差值 |
| assists_diff_JUNGLE | 分位置差值 |
| gold_diff_MIDDLE | 分位置差值 |
| xp_diff_MIDDLE | 分位置差值 |
| level_diff_MIDDLE | 分位置差值 |
| cs_diff_MIDDLE | 分位置差值 |
| kills_diff_MIDDLE | 分位置差值 |
| deaths_diff_MIDDLE | 分位置差值 |
| assists_diff_MIDDLE | 分位置差值 |
| gold_diff_BOTTOM | 分位置差值 |
| xp_diff_BOTTOM | 分位置差值 |
| level_diff_BOTTOM | 分位置差值 |
| cs_diff_BOTTOM | 分位置差值 |
| kills_diff_BOTTOM | 分位置差值 |
| deaths_diff_BOTTOM | 分位置差值 |
| assists_diff_BOTTOM | 分位置差值 |
| gold_diff_UTILITY | 分位置差值 |
| xp_diff_UTILITY | 分位置差值 |
| level_diff_UTILITY | 分位置差值 |
| cs_diff_UTILITY | 分位置差值 |
| kills_diff_UTILITY | 分位置差值 |
| deaths_diff_UTILITY | 分位置差值 |
| assists_diff_UTILITY | 分位置差值 |

## 分位置特征

`gold_diff_TOP` / `xp_diff_TOP` / `level_diff_TOP` / `cs_diff_TOP`，
以及 JUNGLE、MIDDLE、BOTTOM、UTILITY。

## 打野 / 击杀特征

- `kills_diff_<ROLE>` / `deaths_diff_<ROLE>` / `assists_diff_<ROLE>`：分位置累计 KDA 差。
- `kill_lane_<LANE>_diff`：击杀发生在上/中/下/野区的次数差。
- `jungler_zone_<LANE>_diff`：双方打野当前分钟所在区域（上/中/下）的差。
- `jungler_near_<LANE>_diff`：双方打野是否靠近该路（严格路线分类）的差。
- `jungler_gank_<LANE>_diff`：双方打野参与该路击杀的累计次数差。
- `blue_jungle_start_<CAMP>` / `red_jungle_start_<CAMP>`：首清营地 one-hot
  （`blue` / `red` / `wolves` / `raptors`）。
- `*_jungle_start_invade`：首清是否入侵对方野区。

## 英雄

`blue_champ_TOP` 等 10 列是 championId；训练时按 categorical 处理。

## 切分

`split.json` 按 gameId 分组切分，同一局不会同时出现在 train 和 val/test。
