#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
CLI = REPO_ROOT / "grok-video-studio" / "scripts" / "grok_video_studio.py"


AB_CASES: tuple[dict[str, Any], ...] = (
    {
        "id": "quickai-dialogue-closeup",
        "provider": "quickai",
        "title": "QuickAI dialogue closeup",
        "topic": "A woman asks why he made this choice",
        "story": "A woman absorbs a painful answer, controls her emotion, then asks one short question.",
        "role": "closeup",
        "character": {"id": "woman", "name": "Woman", "identity": "An original Chinese woman in her late twenties with natural facial detail and a dark green blouse."},
        "dialogue": {"id": "line-001", "speaker": "woman", "text": "为什么要这样？", "start": 0.2, "end": 3.0, "emotion": "restrained hurt"},
        "video_prompt": "Over-the-shoulder closeup. She listens without speaking, her eyes shift slightly, she suppresses emotion, then looks up and asks the short question once. The unseen listener remains still. Subtle breathing and natural blinking.",
        "performance": {"baseline": "controlled stillness", "trigger": "the unseen listener finishes speaking", "visible_response": "a tiny eye shift and tightened lower eyelid", "suppression": "she steadies her breath", "decision": "she looks up and asks the question"},
        "audio_intent": "dialogue",
        "environment_sound": "quiet apartment room tone and distant city traffic",
    },
    {
        "id": "quickainew-dialogue-closeup",
        "provider": "quickainew",
        "copy": "quickai-dialogue-closeup",
    },
    {
        "id": "quickai-dialogue-positive-clean-frame",
        "provider": "quickai",
        "copy": "quickai-dialogue-closeup",
        "title": "QuickAI dialogue positive clean-frame policy",
    },
    {
        "id": "quickai-rural-summer",
        "provider": "quickai",
        "title": "QuickAI rural summer",
        "topic": "A cinematic summer afternoon in a Chinese village",
        "story": "Heat, wind, water, and ordinary work reveal a summer afternoon without dialogue.",
        "role": "establishing",
        "video_prompt": "Wide cinematic view of an original southern Chinese village in late-summer afternoon. Rice leaves ripple in uneven wind, cicadas pulse, an irrigation channel catches moving sunlight, and a distant farmer slowly crosses a field path. The camera makes a restrained lateral drift while foreground leaves create depth.",
        "performance": {"baseline": "humid stillness", "trigger": "a gust crosses the rice field", "visible_response": "leaves ripple in layers and reflected light breaks on water", "suppression": "the camera remains restrained", "decision": "the distant farmer continues out of frame"},
        "audio_intent": "score-ambience",
        "environment_sound": "dense cicadas, soft wind through rice, irrigation water, one distant bird",
        "audio_notes": "restrained warm instrumental score under natural ambience, no voice",
    },
    {
        "id": "quickainew-rural-summer",
        "provider": "quickainew",
        "copy": "quickai-rural-summer",
    },
    {
        "id": "quickai-action-impact",
        "provider": "quickai",
        "title": "QuickAI action impact",
        "topic": "One readable martial arts impact in a rain-soaked courtyard",
        "story": "A fighter sees an attack, evades, lands one counterstrike, and the environment shows the impact.",
        "role": "wide",
        "video_prompt": "Rain-soaked stone courtyard at night, two original martial artists in clear full-body silhouettes. The attacker commits to one horizontal staff strike from screen left. The defender steps inside the arc, redirects the staff, and lands one shoulder strike. At contact, rainwater bursts from clothing and a wooden rack behind them shudders. End during the defender's recovery step and falling debris, not on a victory pose.",
        "performance": {"baseline": "both fighters measure distance", "trigger": "the staff attack begins", "visible_response": "the defender tracks the weapon and steps inside", "suppression": "no flourish", "decision": "one compact counterstrike"},
        "audio_intent": "effects-ambience",
        "environment_sound": "hard rain on stone, wet footsteps, cloth movement",
        "sound_effects": "staff whoosh, redirect contact, heavy shoulder impact, wooden rack rattle",
        "edit_out": 4.0,
    },
    {
        "id": "quickainew-action-impact",
        "provider": "quickainew",
        "copy": "quickai-action-impact",
    },
    {
        "id": "quickai-single-full-frame-comedy",
        "provider": "quickai",
        "title": "QuickAI single full-frame comedy",
        "topic": "Two neighbors accidentally exchange identical umbrellas",
        "story": "Inside one elevator, two neighbors notice that they picked up each other's identical black umbrellas.",
        "role": "reaction",
        "genre": ["comedy"],
        "characters": [
            {"id": "younger", "name": "Younger neighbor", "identity": "Original younger adult in a navy jacket."},
            {"id": "older", "name": "Older neighbor", "identity": "Original older adult in a light gray coat."},
        ],
        "video_prompt": "One full-frame medium composition inside an apartment elevator contains both neighbors side by side. Each holds one closed black umbrella. They notice the small charm on the wrong handle, exchange one restrained look, and begin swapping the umbrellas. The same single camera composition continues throughout.",
        "performance": {"baseline": "polite stillness", "trigger": "both notice the wrong umbrella charm", "visible_response": "their eyes move from the handles to each other", "suppression": "they keep the reaction sincere", "decision": "they begin one simultaneous swap"},
        "audio_intent": "effects-ambience",
        "environment_sound": "quiet elevator motor and soft building room tone",
        "sound_effects": "one elevator chime and umbrella handles touching",
    },
    {
        "id": "quickainew-single-full-frame-comedy",
        "provider": "quickainew",
        "copy": "quickai-single-full-frame-comedy",
        "title": "QuickAI New single full-frame comedy",
    },
)


def acceptance_shot(
    role: str,
    visible_event: str,
    prompt: str,
    *,
    audio_intent: str = "score-ambience",
    dialogue: tuple[str, str, str] | None = None,
    edit_duration: float = 5.0,
    effects: str = "",
    character_ids: list[str] | None = None,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "role": role,
        "visible_event": visible_event,
        "prompt": prompt,
        "audio_intent": audio_intent,
        "edit_duration": edit_duration,
        "effects": effects,
    }
    if dialogue:
        speaker, text, emotion = dialogue
        value["dialogue"] = {"speaker": speaker, "text": text, "emotion": emotion}
    if character_ids is not None:
        value["character_ids"] = list(character_ids)
    return value


FULL_CASES: tuple[dict[str, Any], ...] = (
    {
        "id": "cross-cao-cao-30s",
        "workflow": "cinematic-short",
        "genre": ["historical"],
        "title": "曹操识破连环计",
        "story": "曹操在江边军营从风向和船链看出危机，命令撤开前锋，却发现远处已有火光。",
        "characters": [
            {"id": "cao-cao", "name": "曹操", "identity": "Late-Han warlord in his fifties, angular face, short beard, dark red command robe and black armor."},
            {"id": "advisor", "name": "谋士", "identity": "Lean middle-aged strategist in a gray robe and black cap."},
        ],
        "shots": [
            acceptance_shot("establishing", "雾中江面与连锁战船建立战场", "Cold dawn over the Yangtze. Chained warships fill the misty river while flags strain in a changing wind. A restrained crane move reveals the command camp on the bank.", effects="river wind, rigging, distant soldiers, restrained war drums"),
            acceptance_shot("insert", "地图火盆与逆转风向形成线索", "Close insert of a period river map beside an oil lamp. The flame suddenly leans the opposite direction; Cao Cao's gloved finger stops above the chained fleet route.", effects="paper, lamp flame, wind entering the tent"),
            acceptance_shot("closeup", "曹操压住惊意作出判断", "Closeup on Cao Cao. He studies the flame, looks toward the river through the tent opening, suppresses alarm, then gives one compact order without a heroic pose.", audio_intent="dialogue", dialogue=("cao-cao", "解开前船，立即后撤。", "controlled urgency")),
            acceptance_shot("wide", "士兵奔向船链执行命令", "Wide lateral view along the wet dock. Soldiers run toward the first chained ships, hauling ropes and iron pins while the fleet rocks unevenly. End during active work.", audio_intent="effects-ambience", effects="boots, chain, shouted work calls, river"),
            acceptance_shot("reaction", "谋士看到远处火光意识到来不及", "Over Cao Cao's shoulder, the advisor looks past him toward tiny orange lights emerging in the river mist. His face tightens; he does not speak.", effects="wind drops, distant alarm begins"),
            acceptance_shot("ending_hook", "火船穿雾逼近", "Long-lens view downriver. Several fire ships break through the fog and grow rapidly larger while foreground soldiers turn toward them. End on advancing fire and rising alarm, not a frozen character pose.", audio_intent="effects-ambience", effects="roaring fire, alarm gong, river wind", character_ids=[]),
        ],
    },
    {
        "id": "cross-flood-lifebuoy-30s",
        "workflow": "cinematic-short",
        "genre": ["disaster"],
        "title": "涨潮中的最后一个救生圈",
        "story": "海水淹入小镇码头，三名陌生人在争抢最后一个救生圈时发现一名儿童被困，最终改变选择。",
        "characters": [
            {"id": "woman", "name": "女人", "identity": "Original woman in her thirties, wet yellow rain jacket, tied black hair."},
            {"id": "man", "name": "男人", "identity": "Original exhausted man in a dark blue work jacket."},
        ],
        "shots": [
            acceptance_shot("establishing", "海水越过台阶淹入码头街道", "Wide disaster geography at a coastal town dock. Seawater surges over the last dry steps, floating crates collide, and stranded people retreat toward a warehouse roof.", audio_intent="effects-ambience", effects="surging water, wind, debris, distant alarms"),
            acceptance_shot("insert", "最后一个救生圈被水冲到两人之间", "Low insert at water level. One orange lifebuoy spins between broken boards and strikes a railing exactly between the woman and the man.", audio_intent="effects-ambience", effects="water impact, plastic ring on metal"),
            acceptance_shot("wide", "两人同时扑向救生圈", "Readable wide shot with the woman screen left and the man screen right. Both wade toward the same lifebuoy, each grabbing one side as another wave raises the water.", audio_intent="effects-ambience", effects="splashes, strained breath, debris"),
            acceptance_shot("reaction", "女人听见儿童呼救松开手", "Close reaction on the woman as a child's off-screen cry cuts through the storm. Her grip tightens, then she looks toward a half-submerged vehicle and releases the ring.", effects="storm, one distant child call, no dialogue from visible adults"),
            acceptance_shot("medium", "男人把救生圈推向儿童", "Medium action. The man follows her gaze, stops pulling, and pushes the lifebuoy through the water toward the trapped child while the woman braces against the current.", audio_intent="effects-ambience", effects="water, heavy breathing, metal creak"),
            acceptance_shot("ending_hook", "更高海浪逼近众人开始协作", "Wide from behind the group. They link arms and move toward higher ground as a larger wall of water crosses the far street. End during urgent coordinated movement.", audio_intent="effects-ambience", effects="rising surge, alarm, tense score"),
        ],
    },
    {
        "id": "cross-family-argument-15s",
        "workflow": "dialogue-scene",
        "genre": ["family"],
        "title": "没关的厨房灯",
        "story": "夫妻因一件小事争执，真正的问题在一句没说出口的话和沉默反应中显现。",
        "characters": [
            {"id": "wife", "name": "妻子", "identity": "Original woman in her early thirties, beige home cardigan, tired but composed."},
            {"id": "husband", "name": "丈夫", "identity": "Original man in his mid thirties, rolled gray shirt sleeves, restrained frustration."},
        ],
        "shots": [
            acceptance_shot("establishing", "深夜厨房和未收拾的餐桌建立矛盾", "Quiet late-night apartment kitchen. One light remains on above an untouched dinner, bills and a child's cup sit on the table. The wife stands at the sink while the husband enters and stops.", edit_duration=5.0),
            acceptance_shot("closeup", "妻子从小事转向真正委屈", "Over the husband's shoulder, close on the wife. She keeps washing one cup, then stops, looks up, and says one restrained sentence instead of shouting.", audio_intent="dialogue", dialogue=("wife", "我在意的从来不是这盏灯。", "restrained exhaustion"), edit_duration=5.0, character_ids=["wife"]),
            acceptance_shot("reaction", "丈夫准备反驳却沉默", "Close reaction on the husband. He opens his mouth to answer, notices the untouched dinner and child's cup, lowers his eyes, and says nothing. End in the unresolved breath before any sigh.", edit_duration=5.0, character_ids=["husband"]),
        ],
    },
    {
        "id": "cross-rural-summer-15s",
        "workflow": "silent-cinema",
        "genre": ["rural"],
        "title": "乡村盛夏午后",
        "story": "不靠对白，用热浪、蝉鸣、水与劳动讲出盛夏午后的时间流动。",
        "shots": [
            acceptance_shot("establishing", "稻田热浪与远处村庄建立季节", "Wide summer rice fields under high afternoon sun. Heat shimmer softens distant tiled roofs while uneven wind travels across the crop in visible bands.", edit_duration=5.0),
            acceptance_shot("insert", "井水西瓜与蝉蜕表现触感", "Tactile close insert beside an old stone well. Cool water runs over a watermelon in a wooden basin; a cicada shell clings to the shaded rim and droplets keep moving.", edit_duration=5.0),
            acceptance_shot("transition", "阵雨影子越过田野改变光线", "High wide view as the shadow of a brief summer cloud crosses the field, wind lifts laundry near a farmhouse, and the first large raindrops darken a dusty path.", edit_duration=5.0),
        ],
    },
    {
        "id": "cross-why-closeup-10s",
        "workflow": "dialogue-scene",
        "genre": ["family"],
        "title": "为什么要这样",
        "story": "一个短问题由过肩、微表情特写和男方无声反应组成。",
        "characters": [
            {"id": "woman", "name": "女生", "identity": "Original Chinese woman in her late twenties, dark green blouse, restrained hurt."},
            {"id": "man", "name": "男生", "identity": "Original Chinese man in his early thirties, charcoal shirt, guarded expression."},
        ],
        "shots": [
            acceptance_shot("over_shoulder", "男方背影与女生沉默建立关系距离", "Over the man's shoulder in a quiet apartment, the woman looks at him without speaking. Her eyes search his face while city light moves softly behind her.", edit_duration=3.333, character_ids=["woman"]),
            acceptance_shot("closeup", "女生压住情绪问出关键一句", "Tight closeup on the woman. She looks down for a fraction, steadies her breath, raises her eyes, and asks one short question with controlled hurt.", audio_intent="dialogue", dialogue=("woman", "为什么要这样？", "restrained hurt"), edit_duration=3.333, character_ids=["woman"]),
            acceptance_shot("reaction", "男方无声反应没有回答", "Close reaction on the man. He begins to answer, stops, and looks away as the meaning lands. No spoken words and no theatrical sigh.", edit_duration=3.334, character_ids=["man"]),
        ],
    },
    {
        "id": "cross-fight-15s",
        "workflow": "action-scene",
        "genre": ["wuxia"],
        "title": "雨巷突围",
        "story": "一名护送者在雨巷遭三人围攻，动作被拆为发现、闪避、撞击、反应和突围。",
        "characters": [{"id": "fighter", "name": "护送者", "identity": "Original lean martial artist in a dark indigo coat, short tied hair, carrying a wrapped wooden case."}],
        "shots": [
            acceptance_shot("wide", "三名袭击者封住雨巷前后", "Wide geography in a narrow rain alley. The indigo-coated courier stops center frame as three attackers close the front and rear exits. Everyone's position and screen direction remain clear.", audio_intent="effects-ambience", edit_duration=3.0, effects="rain, footsteps, distant thunder"),
            acceptance_shot("closeup", "护送者从水中倒影发现背后攻击", "Close insert-to-reaction: in a puddle reflection, a rear attacker raises a short staff. The courier's eyes shift before his body moves.", audio_intent="effects-ambience", edit_duration=3.0, effects="rain drops, cloth tension, staff lift"),
            acceptance_shot("medium", "护送者侧身闪避第一击", "Medium side view. One staff strike crosses from screen left; the courier pivots once, keeps the wrapped case protected, and lets the weapon miss by inches.", audio_intent="effects-ambience", edit_duration=3.0, effects="staff whoosh, wet foot pivot"),
            acceptance_shot("insert", "肘击接触点与木墙破裂", "Tight action insert. The courier lands one compact elbow counter; at contact the attacker hits a thin wooden stall wall and rainwater and splinters burst outward.", audio_intent="effects-ambience", edit_duration=3.0, effects="body impact, wood crack, debris"),
            acceptance_shot("ending_hook", "护送者在其余两人扑来前冲出侧门", "Wide cut-on-action. While two attackers react to falling boards, the courier drives through a side gate with the case secure. End during the sprint and collapsing stall, no victory pose.", audio_intent="effects-ambience", edit_duration=3.0, effects="running feet, boards falling, rain"),
        ],
    },
)

FULL_CASES += (
    {
        "id": "module-t2v-native-10s",
        "workflow": "text-to-video",
        "title": "T2V 原生声音",
        "story": "一名古代驿使在暴雨前送出最后一封信。",
        "characters": [{"id": "courier", "name": "驿使", "identity": "Original young Han-era courier in a soaked brown travel cloak."}],
        "generation_seconds": 10,
        "shots": [acceptance_shot("medium", "驿使把信交给守门人后继续赶路", "Cinematic medium shot at an ancient gate before a storm. The soaked courier runs in, hands one sealed letter to an off-screen guard, checks the dark sky, and immediately continues through the gate. End while he is still running.", audio_intent="effects-ambience", edit_duration=10.0, effects="wind, first rain, boots, paper and gate wood")],
    },
    {
        "id": "module-i2v-native-10s",
        "workflow": "single-image-animation",
        "mode": "image-to-video",
        "title": "I2V 原生声音",
        "story": "一张批准人物帧产生克制的无对白反应。",
        "generation_seconds": 10,
        "shots": [acceptance_shot("closeup", "人物听到门外脚步后转移视线", "Preserve the exact supplied woman, green blouse, face, room, light, and composition. She hears footsteps outside the room, shifts her eyes toward the door, draws one controlled breath, and turns only a few degrees. No speech and no scene change.", edit_duration=10.0)],
    },
    {
        "id": "module-character-story-15s",
        "workflow": "character-consistent-story",
        "mode": "image-to-video",
        "title": "角色一致性故事",
        "story": "同一名女子在三个镜头中从等待、发现线索到作出决定。",
        "shots": [
            acceptance_shot("establishing", "她独自在客厅等待", "Preserve the supplied woman's identity and green blouse. Medium-wide apartment view; she waits near the window while evening traffic light moves outside.", edit_duration=5.0),
            acceptance_shot("reaction", "她看到桌上遗留的钥匙", "Preserve identity and clothing. Close reaction as she notices a single key on the table, her eyes narrow slightly, and she reaches toward it without touching yet.", edit_duration=5.0),
            acceptance_shot("ending_hook", "她拿起钥匙走向门口", "Preserve identity and clothing. Medium profile; she takes the key, turns toward the apartment door, and begins walking. End during the first committed step.", edit_duration=5.0),
        ],
    },
    {
        "id": "module-cinematic-sci-fi-20s",
        "workflow": "cinematic-short",
        "genre": ["sci-fi", "suspense"],
        "title": "月面温室停电",
        "story": "月面温室突然失电，工程师通过植物叶片上的霜判断真正的泄漏位置。",
        "characters": [{"id": "engineer", "name": "工程师", "identity": "Original Asian lunar engineer in a practical white utility suit, uncovered face, short black hair."}],
        "shots": [
            acceptance_shot("establishing", "月面温室与地球建立空间", "Wide lunar greenhouse interior with Earth beyond reinforced glass. Grow lights shut off in sequence and emergency red strips remain.", audio_intent="effects-ambience", edit_duration=5.0, effects="fans winding down, alarm, suit movement"),
            acceptance_shot("insert", "叶片边缘结霜暴露气流", "Macro insert of a green leaf. Frost advances along only one edge while loose dust drifts toward a floor seam.", audio_intent="effects-ambience", edit_duration=5.0, effects="air hiss, faint alarm"),
            acceptance_shot("reaction", "工程师理解线索", "Close on the engineer following the frost direction with her eyes, controlling fear, then looking toward the floor seam.", edit_duration=5.0),
            acceptance_shot("ending_hook", "她封住裂缝但外部影子掠过", "Low medium action as she clamps a seal over the floor seam and pressure stabilizes; through the glass, an unexplained shadow crosses the lunar surface. End on her head turning toward it.", audio_intent="effects-ambience", edit_duration=5.0, effects="seal clamp, air hiss stopping, low alert tone"),
        ],
    },
    {
        "id": "module-dialogue-scene-15s",
        "workflow": "dialogue-scene",
        "genre": ["romance"],
        "title": "车站告别",
        "story": "两人在车站告别，一句短话和听者反应改变了关系。",
        "characters": [
            {"id": "woman", "name": "女人", "identity": "Original woman in a navy coat holding one train ticket."},
            {"id": "man", "name": "男人", "identity": "Original man in a charcoal jacket carrying a small canvas bag."},
        ],
        "shots": [
            acceptance_shot("establishing", "空站台与即将进站的列车建立期限", "Blue-hour rural station platform. The woman and man stand several steps apart as a distant headlight approaches through drizzle.", edit_duration=5.0),
            acceptance_shot("closeup", "女人说出真正选择", "Over the man's shoulder, close on the woman. She glances at the ticket, meets his eyes, and speaks one quiet line.", audio_intent="dialogue", dialogue=("woman", "我不是在等车，我在等你开口。", "quiet resolve"), edit_duration=5.0),
            acceptance_shot("reaction", "男人放下行李向前一步", "Close reaction on the man. He absorbs the sentence, releases his grip on the canvas bag, and takes one small step toward her without speaking.", edit_duration=5.0),
        ],
    },
    {
        "id": "module-silent-cinema-15s",
        "workflow": "silent-cinema",
        "genre": ["suspense"],
        "title": "清晨空办公室",
        "story": "空办公室通过一杯仍冒热气的咖啡和自动打开的门讲出有人刚刚离开。",
        "shots": [
            acceptance_shot("establishing", "清晨空办公室建立无人空间", "Wide predawn office, empty desks, city rain on windows, only one task lamp on. The camera slowly advances along the aisle.", edit_duration=5.0),
            acceptance_shot("insert", "热咖啡与转动椅子显示刚有人在", "Close insert of a coffee cup still steaming beside a chair that rotates slightly and a wet footprint on the floor.", edit_duration=5.0),
            acceptance_shot("transition", "远端门自动打开但无人出现", "Long hallway composition. The far access door unlocks and opens slowly; fluorescent light enters, but no person appears before the cut.", edit_duration=5.0),
        ],
    },
    {
        "id": "module-action-scene-15s",
        "workflow": "action-scene",
        "genre": ["wuxia"],
        "title": "仓库夺刀",
        "story": "狭窄仓库内的威胁、闪避、夺刀和逃离被拆成四个动作节点。",
        "shots": [
            acceptance_shot("wide", "对手在货架间形成夹击", "Wide warehouse geography. One courier is trapped between two attackers and tall wooden shelves; exits and positions are clear.", audio_intent="effects-ambience", edit_duration=3.75, effects="warehouse room tone, shoes, wood creak"),
            acceptance_shot("medium", "第一刀挥来主角贴架闪避", "Medium profile. One attacker makes a single downward knife strike; the courier pivots against the shelf and the blade bites into wood.", audio_intent="effects-ambience", edit_duration=3.75, effects="knife whoosh, blade in wood, shelf rattle"),
            acceptance_shot("insert", "主角控制手腕夺下刀", "Tight action insert on hands and forearms. The courier traps the attacker's wrist, twists once, and the knife drops onto a crate.", audio_intent="effects-ambience", edit_duration=3.75, effects="grip, metal on wood, strained breath"),
            acceptance_shot("ending_hook", "第二人撞倒货架主角冲出侧门", "Wide cut-on-action. The second attacker collides with a shelf; boxes fall as the courier runs through a side door. End during falling boxes and his sprint.", audio_intent="effects-ambience", edit_duration=3.75, effects="boxes falling, running feet, door impact"),
        ],
    },
    {
        "id": "module-performance-i2v-10s",
        "workflow": "dance-performance",
        "mode": "image-to-video",
        "title": "雨后台阶独舞",
        "story": "一名原创舞者完成一段连续、可读的现代舞动作。",
        "generation_seconds": 10,
        "shots": [acceptance_shot("wide", "舞者从静止展开并穿过湿台阶", "Full-body wide shot of an original dancer on wet stone steps after rain. She begins still, extends both arms in one controlled arc, crosses down two steps, turns once, and continues into a low reach. Keep limbs anatomically coherent and end during movement.", edit_duration=10.0, effects="soft shoes on wet stone, light rain drips, restrained contemporary music")],
    },
    {
        "id": "module-general-video-10s",
        "workflow": "general-video",
        "title": "通用视频路线",
        "story": "一个单镜头同时验证自然动作、镜头运动和原生环境声。",
        "generation_seconds": 10,
        "shots": [acceptance_shot("medium", "修伞匠在雨棚下完成一次开合检查", "Feature-film medium shot under a rainy market awning. An original elderly umbrella repairer tightens one spoke, opens the repaired umbrella once, checks the fabric against the light, and continues adjusting the handle as rain falls behind him.", audio_intent="effects-ambience", edit_duration=10.0, effects="steady rain, umbrella fabric, small metal tool clicks")],
    },
    {
        "id": "module-comedy-scene-15s",
        "workflow": "comedy-scene",
        "genre": ["comedy"],
        "title": "电梯里的同款雨伞",
        "story": "两人误拿同款雨伞，通过道具插入和反应完成笑点。",
        "characters": [
            {"id": "office-worker", "name": "上班族", "identity": "Original young office worker in a navy jacket."},
            {"id": "neighbor", "name": "邻居", "identity": "Original older neighbor in a light gray coat."},
        ],
        "shots": [
            acceptance_shot("establishing", "两人带着相同黑伞进入电梯", "Apartment elevator lobby after rain. A young office worker and older neighbor enter from opposite sides carrying identical closed black umbrellas.", edit_duration=5.0),
            acceptance_shot("insert", "两把伞在角落交换位置", "Low prop insert inside the elevator. The two identical umbrellas lean together; a small floor jolt makes them cross and swap positions.", edit_duration=5.0, effects="elevator ding, umbrella handles tapping"),
            acceptance_shot("reaction", "两人拿错后同时发现又默默换回", "Two-shot reaction. Each picks up the wrong umbrella, notices the other's personalized charm, looks at the other, and silently swaps back at exactly the same time. Play sincerely, no exaggerated mugging.", edit_duration=5.0),
        ],
    },
    {
        "id": "module-product-ad-15s",
        "workflow": "product-ad",
        "title": "无品牌保温杯广告",
        "story": "用材质、蒸汽和实际使用展示一只无品牌不锈钢保温杯。",
        "shots": [
            acceptance_shot("establishing", "清晨窗边产品建立", "Premium cinematic product shot of one unbranded brushed stainless steel thermos on a dark kitchen counter at sunrise, accurate cylindrical form, no logo or text. Light moves across the metal.", edit_duration=5.0),
            acceptance_shot("insert", "热水倒入与蒸汽展示保温用途", "Macro insert preserving the exact thermos shape. Hot tea pours into the open cup, steam curls through warm side light, and no liquid spills.", edit_duration=5.0, effects="tea pouring, gentle ceramic contact"),
            acceptance_shot("ending_hook", "户外清晨手拿杯子延续使用场景", "Medium lifestyle shot of the same unbranded thermos in gloved hands at a cold overlook, accurate shape and brushed finish. The lid opens and steam moves into the wind; end on moving steam.", edit_duration=5.0, effects="mountain wind, lid click, restrained score"),
        ],
    },
    {
        "id": "module-upstream-captions-10s",
        "workflow": "text-to-video",
        "title": "显式上游字幕",
        "story": "一名原创主持人说一句短消息，并明确要求上游字幕。",
        "subtitle_source": "upstream",
        "generation_seconds": 10,
        "characters": [{"id": "host", "name": "主持人", "identity": "Original Chinese woman presenter in a simple blue studio jacket."}],
        "shots": [acceptance_shot("medium", "主持人自然说出一句短消息", "Clean medium studio shot. The host looks into camera, takes a natural breath, and speaks one short sentence once with clear mouth movement.", audio_intent="dialogue", dialogue=("host", "今天的重点，是先确认事实。", "calm authority"), edit_duration=10.0)],
    },
    {
        "id": "module-scene-animation-10s",
        "workflow": "scene-animation",
        "mode": "image-to-video",
        "title": "场景图分层动效",
        "story": "单张人物场景图保持构图，只产生前中后景的轻微运动。",
        "generation_seconds": 10,
        "shots": [acceptance_shot("medium", "人物与背景产生分层自然运动", "Preserve the exact supplied portrait composition and identity. Foreground hair moves slightly, the woman blinks and breathes, soft window light shifts in the middle ground, and the distant room remains stable. No camera orbit and no scene change.", edit_duration=10.0)],
    },
    {
        "id": "module-comedy-action-10s",
        "workflow": "comedy-action",
        "mode": "image-to-video",
        "title": "单图轻喜剧反应",
        "story": "同一人物听到门外奇怪声音后做克制的喜剧反应。",
        "generation_seconds": 10,
        "shots": [acceptance_shot("closeup", "人物从认真到怀疑再恢复镇定", "Preserve the exact supplied woman and room. She hears one off-screen squeak, looks toward it, briefly raises one eyebrow, then tries to return to composure while another quieter squeak interrupts her. Natural restrained comedy, no exaggerated face.", edit_duration=10.0, effects="quiet room tone, two small off-screen rubber squeaks")],
    },
    {
        "id": "module-short-drama-15s",
        "workflow": "short-drama",
        "genre": ["family"],
        "title": "遗失的钱包",
        "story": "外卖员归还钱包，屋主从警惕到理解，全程只说一句必要对白。",
        "characters": [
            {"id": "courier", "name": "外卖员", "identity": "Original young courier in a plain yellow rain jacket, no logo."},
            {"id": "resident", "name": "住户", "identity": "Original middle-aged resident in a dark home sweater."},
        ],
        "shots": [
            acceptance_shot("establishing", "雨夜门口外卖员拿着钱包等待", "Rainy apartment doorway at night. An unbranded courier stands outside holding a wet brown wallet while the resident opens the door only partway.", edit_duration=5.0),
            acceptance_shot("closeup", "外卖员说明来意", "Over the resident's shoulder, close on the courier. He offers the wallet with both hands and speaks one short line, slightly out of breath from the rain.", audio_intent="dialogue", dialogue=("courier", "您落在楼下了，我怕里面的证件淋湿。", "earnest and breathless"), edit_duration=5.0),
            acceptance_shot("reaction", "住户看到钱包里的旧照片放下戒备", "Close reaction on the resident opening the wallet enough to see an old family photo. His guarded expression softens and he opens the door wider without speaking.", edit_duration=5.0),
        ],
    },
)


def run_cli(*args: str) -> dict[str, Any]:
    result = subprocess.run(
        [sys.executable, str(CLI), *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    if result.returncode:
        raise RuntimeError(result.stdout or result.stderr)
    return json.loads(result.stdout)


def resolved_case(raw: dict[str, Any], cases: dict[str, dict[str, Any]]) -> dict[str, Any]:
    parent_id = str(raw.get("copy", ""))
    if not parent_id:
        return dict(raw)
    if parent_id not in cases:
        raise ValueError(f"unknown copied case: {parent_id}")
    value = {**resolved_case(cases[parent_id], cases), **raw}
    value.pop("copy", None)
    return value


def prepare_case(root: Path, case: dict[str, Any]) -> dict[str, Any]:
    project_root = root / str(case["id"])
    if not (project_root / "project.json").is_file():
        run_cli(
            "init",
            str(project_root),
            "--title",
            str(case["title"]),
            "--topic",
            str(case["topic"]),
            "--workflow",
            "text-to-video",
            "--mode",
            "text-to-video",
            "--shots",
            "1",
            "--seconds",
            "6",
            "--video-size",
            "720x1280",
            "--aspect-ratio",
            "9:16",
            "--video-resolution",
            "480p",
            "--video-provider",
            str(case["provider"]),
        )
    path = project_root / "project.json"
    project = json.loads(path.read_text(encoding="utf-8"))
    project["story"] = str(case["story"])
    project["style_bible"] = (
        "Feature-film naturalism, physically grounded motion, natural facial detail, "
        "edge-to-edge photographed physical space, one uninterrupted camera-original image."
    )
    # These A/B fixtures intentionally exercise the risky T2V route so the paid
    # results can be compared with the default blocking policy.
    project["layout_risk_policy"] = "allow"
    project["characters"] = list(case.get("characters", [])) or ([case["character"]] if case.get("character") else [])
    project["director"]["genre_packs"] = list(case.get("genre", []))
    shot = project["shots"][0]
    shot.update(
        {
            "summary": str(case["story"]),
            "shot_role": str(case["role"]),
            "character_ids": [str(item["id"]) for item in project["characters"]],
            "generate_image": False,
            "use_character_master": False,
            "image_prompt": "",
            "video_prompt": str(case["video_prompt"]),
            "dialogue": [case["dialogue"]] if case.get("dialogue") else [],
            "audio_intent": str(case["audio_intent"]),
            "environment_sound": str(case.get("environment_sound", "")),
            "sound_effects": str(case.get("sound_effects", "")),
            "audio_notes": str(case.get("audio_notes", "")),
            "performance": dict(case["performance"]),
            "camera": "cinematic coverage with stable subject framing",
            "camera_motion": "motivated restrained motion",
            "exit_action": "visible motion continues through the edit point",
            "exit_behavior": "cut-on-action",
            "edit_in": 0.2,
            "edit_out": float(case.get("edit_out", 5.6)),
            "timeline_duration": float(case.get("edit_out", 5.6)) - 0.2,
        }
    )
    path.write_text(json.dumps(project, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    validation = run_cli("validate", str(project_root))
    return {"id": case["id"], "provider": case["provider"], "project": str(project_root), "valid": validation["ok"]}


def prepare_i2v_source(root: Path) -> Path:
    project_root = root / "i2v-source-keyframe"
    if not (project_root / "project.json").is_file():
        run_cli(
            "init", str(project_root), "--title", "I2V source keyframe", "--topic", "Original courier at a rainy station",
            "--workflow", "general-video", "--mode", "image-to-video", "--shots", "1", "--seconds", "6",
            "--video-size", "720x1280", "--aspect-ratio", "9:16", "--video-resolution", "480p",
        )
    path = project_root / "project.json"
    project = json.loads(path.read_text(encoding="utf-8"))
    project["story"] = "An original courier notices the last train arriving through rain."
    project["style_bible"] = "Cinematic naturalism, realistic wet surfaces, no text, logos, watermarks, or interface elements."
    shot = project["shots"][0]
    shot.update(
        {
            "summary": "A courier waits under the station canopy.",
            "shot_role": "medium",
            "generate_image": True,
            "use_character_master": False,
            "image_prompt": "Vertical cinematic medium shot of an original Chinese woman courier in a mustard raincoat under an old rural railway station canopy at blue hour, wet platform reflections, a folded paper ticket in her hand, train light far in the background, natural face, realistic hands, no readable text.",
            "video_prompt": "She notices a distant train light, tightens her grip on the folded ticket, and turns slightly toward the track while rain continues beyond the canopy.",
            "audio_intent": "score-ambience",
        }
    )
    path.write_text(json.dumps(project, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    run_cli("validate", str(project_root))
    return project_root


def prepare_layout_i2v_case(root: Path) -> dict[str, Any]:
    project_root = root / "quickai-i2v-single-full-frame-comedy"
    if not (project_root / "project.json").is_file():
        run_cli(
            "init", str(project_root), "--title", "QuickAI I2V single full-frame comedy",
            "--topic", "Two neighbors exchange identical umbrellas in one full-frame composition",
            "--workflow", "single-image-animation", "--mode", "image-to-video", "--shots", "1", "--seconds", "6",
            "--video-size", "720x1280", "--aspect-ratio", "9:16", "--video-resolution", "480p", "--video-provider", "quickai",
        )
    path = project_root / "project.json"
    project = json.loads(path.read_text(encoding="utf-8"))
    project["story"] = "Inside one elevator, two neighbors discover they picked up each other's identical umbrellas."
    project["director"]["genre_packs"] = ["comedy"]
    project["characters"] = [
        {"id": "younger", "name": "Younger neighbor", "identity": "Original younger adult in a navy jacket."},
        {"id": "older", "name": "Older neighbor", "identity": "Original older adult in a light gray coat."},
    ]
    project["character_master"].update({"enabled": False, "generate": False})
    shot = project["shots"][0]
    shot.update(
        {
            "summary": project["story"], "shot_role": "reaction", "character_ids": ["younger", "older"],
            "generate_image": True, "use_character_master": False,
            "image_prompt": "One vertical full-frame cinematic medium composition inside an apartment elevator. Both original neighbors stand side by side in the same physical camera view, the younger adult in a navy jacket and the older adult in a light gray coat. Each holds one closed black umbrella with a different small handle charm. Natural faces and hands, elevator walls filling the background.",
            "video_prompt": "Preserve the supplied single full-frame composition, both people, clothes, elevator, and umbrella handles. They notice the charm on the wrong handle, exchange one restrained look, and begin one simultaneous umbrella swap. Keep the same camera and physical scene throughout.",
            "audio_intent": "effects-ambience", "environment_sound": "quiet elevator motor and soft building room tone",
            "sound_effects": "one elevator chime and umbrella handles touching",
            "performance": {"baseline": "polite stillness", "trigger": "both notice the wrong handle charm", "visible_response": "their eyes move from the handles to each other", "suppression": "sincere restrained reaction", "decision": "begin one simultaneous swap"},
            "camera": "one stable medium composition", "camera_motion": "locked camera", "exit_action": "the swap continues into the cut",
            "exit_behavior": "cut-on-action", "edit_in": 0.2, "edit_out": 5.6, "timeline_duration": 5.4,
        }
    )
    path.write_text(json.dumps(project, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    validation = run_cli("validate", str(project_root))
    return {"id": project_root.name, "provider": "quickai", "project": str(project_root), "valid": validation["ok"]}


I2V_FALLBACK_CASES: tuple[dict[str, Any], ...] = (
    {
        "id": "fallback-family-dialogue-i2v",
        "title": "家庭争吵单帧 I2V 回退",
        "topic": "A restrained wife reveals the real reason for an argument",
        "role": "closeup",
        "character": {"id": "wife", "name": "妻子", "identity": "Original Chinese woman in her early thirties, beige home cardigan, tired but composed."},
        "image_prompt": (
            "One vertical full-frame cinematic over-the-shoulder closeup in a lived-in apartment kitchen at night. "
            "An original Chinese woman in her early thirties wears a beige home cardigan and holds one plain ceramic cup. "
            "Only the soft out-of-focus shoulder edge of her husband appears in the near foreground. One physical camera view, "
            "natural face and hands, sink and untouched dinner behind her, no text, captions, logos, interface, borders, panels, or collage."
        ),
        "video_prompt": (
            "Preserve the supplied single full-frame composition, woman, cardigan, cup, kitchen, light, and foreground shoulder. "
            "She keeps her gaze on the cup, pauses, raises her eyes toward the listener, and speaks one restrained sentence. "
            "Her hurt remains controlled; no sigh, pose, camera change, inserted view, repeated frame, split screen, or visible words."
        ),
        "dialogue": {"speaker": "wife", "text": "我在意的从来不是这盏灯。", "emotion": "restrained exhaustion"},
        "audio_intent": "dialogue",
        "environment_sound": "quiet late-night kitchen room tone and distant traffic",
        "effects": "ceramic cup touching the sink once",
        "performance": {
            "baseline": "tired composure",
            "trigger": "the listener dismisses the argument as a trivial light",
            "visible_response": "her eyes leave the cup and meet the listener",
            "suppression": "she controls anger and avoids theatrical mouth or head motion",
            "decision": "she states the deeper grievance without resolving it",
        },
    },
    {
        "id": "fallback-why-closeup-i2v",
        "title": "为什么要这样单帧 I2V 回退",
        "topic": "A woman asks one painful question in a restrained closeup",
        "role": "closeup",
        "character": {"id": "woman", "name": "女生", "identity": "Original Chinese woman in her late twenties, dark green blouse, restrained hurt."},
        "image_prompt": (
            "One vertical full-frame cinematic tight closeup of an original Chinese woman in her late twenties wearing a dark green blouse. "
            "She faces an unseen listener just off camera in a quiet apartment at blue hour; only a narrow soft shoulder edge may enter foreground. "
            "Natural skin and eyes, restrained hurt, one physical camera view, no text, captions, logos, interface, borders, panels, collage, or duplicate face."
        ),
        "video_prompt": (
            "Preserve the exact supplied woman, green blouse, apartment light, closeup, and single full-frame camera view. "
            "She lowers her eyes briefly, steadies one breath, raises her gaze to the listener, and asks one short question with controlled hurt. "
            "No crying performance, sigh, final pose, camera change, inserted view, repeated frame, split screen, or visible words."
        ),
        "dialogue": {"speaker": "woman", "text": "为什么要这样？", "emotion": "restrained hurt"},
        "audio_intent": "dialogue",
        "environment_sound": "quiet apartment room tone and distant city traffic",
        "effects": "",
        "performance": {
            "baseline": "restrained hurt",
            "trigger": "the listener remains silent",
            "visible_response": "a small downward glance and a steadied breath",
            "suppression": "she contains tears and avoids exaggerated mouth movement",
            "decision": "she raises her eyes and asks the question",
        },
    },
    {
        "id": "fallback-cao-fire-ships-i2v",
        "title": "曹操火船收尾单帧 I2V 回退",
        "topic": "Fire ships advance through fog toward a chained fleet",
        "role": "ending_hook",
        "character": None,
        "image_prompt": (
            "One vertical full-frame cinematic long-lens view across the ancient Yangtze at dawn. Several period fire ships emerge through dense river fog, "
            "orange flames and black smoke advancing toward a chained fleet, wet wooden dock and a few tiny period soldiers only at the bottom edge. "
            "Historically inspired physical scene, one camera-original image, no modern objects, text, captions, logos, app interface, buttons, borders, panels, or collage."
        ),
        "video_prompt": (
            "Preserve the supplied single full-frame river composition, period ships, fog, dock, and dawn light. Fire ships advance visibly through the fog, "
            "flames stream with the wind, foreground soldiers turn toward the threat, and the alarm rises. End during advancing motion, with no camera change, "
            "inserted view, repeated frame, split screen, modern interface, or visible words."
        ),
        "audio_intent": "effects-ambience",
        "environment_sound": "river wind, rigging, and distant military alarm",
        "effects": "roaring fire, alarm gong, hurried period soldiers",
        "performance": {
            "baseline": "fog-obscured river threat",
            "trigger": "the fire ships break through the fog",
            "visible_response": "foreground soldiers turn and begin moving",
            "suppression": "no heroic pose or modern spectacle framing",
            "decision": "the threat keeps advancing into the cut",
        },
    },
)

I2V_FALLBACK_CASES += (
    {
        "id": "fallback-family-establishing-i2v", "title": "家庭争吵建立镜头 I2V 回退",
        "topic": "A late-night kitchen establishes unresolved distance between a couple", "role": "establishing",
        "characters": [
            {"id": "wife", "name": "妻子", "identity": "Original Chinese woman in her early thirties, beige cardigan."},
            {"id": "husband", "name": "丈夫", "identity": "Original Chinese man in his mid thirties, gray shirt."},
        ],
        "image_prompt": "One vertical full-frame cinematic medium-wide view of a lived-in apartment kitchen at night. An original Chinese wife in a beige cardigan stands at the sink screen left; her husband in a gray shirt has just entered screen right. An untouched dinner, bills, and a child's cup sit between them. One physical camera view, no words, borders, panels, collage, logos, or interface.",
        "video_prompt": "Preserve the supplied single full-frame kitchen, couple, clothes, table props, light, and screen positions. The wife keeps washing one cup; the husband enters, notices the untouched dinner, and stops at a meaningful distance. Neither speaks. Continue natural task motion into the cut; no camera change, inserted view, split screen, repeated panel, or visible words.",
        "audio_intent": "score-ambience", "environment_sound": "quiet kitchen room tone, water, and distant traffic", "effects": "faucet, one footstep, ceramic cup",
        "performance": {"baseline": "tired routine", "trigger": "the husband sees the untouched dinner", "visible_response": "he slows while she keeps working", "suppression": "neither performs anger for camera", "decision": "they remain apart as the task continues"},
    },
    {
        "id": "fallback-why-man-reaction-i2v", "title": "为什么要这样男方反应 I2V 回退",
        "topic": "A man cannot answer one painful question", "role": "reaction",
        "character": {"id": "man", "name": "男生", "identity": "Original Chinese man in his early thirties, charcoal shirt, guarded expression."},
        "image_prompt": "One vertical full-frame cinematic close reaction of an original Chinese man in his early thirties wearing a charcoal shirt, facing an unseen woman just off camera in a quiet apartment. Natural face, guarded eyes, soft blue-hour window light, one physical camera view, no text, captions, borders, panels, collage, logos, or interface.",
        "video_prompt": "Preserve the supplied man, charcoal shirt, apartment, light, and single full-frame closeup. He begins to answer, stops before making sound, lets his eyes move away, then looks back without a theatrical sigh. End during the unresolved breath; no camera change, inserted view, split screen, repeated panel, or visible words.",
        "audio_intent": "score-ambience", "environment_sound": "quiet apartment room tone and distant city traffic", "effects": "",
        "performance": {"baseline": "guarded stillness", "trigger": "the unseen woman asks why", "visible_response": "his lips part then stop and his gaze slips away", "suppression": "no speech or theatrical sigh", "decision": "he remains unable to answer"},
    },
    {
        "id": "fallback-cao-dock-action-i2v", "title": "曹操码头行动 I2V 回退",
        "topic": "Period soldiers race to release chained warships", "role": "wide", "character": None,
        "image_prompt": "One vertical full-frame cinematic wide lateral view along a wet ancient Yangtze military dock at dawn. Period soldiers work around chained wooden warships, ropes and iron pins clearly visible, mist and flags in the background. One physical historical camera view, no modern objects, words, borders, panels, collage, logos, or interface.",
        "video_prompt": "Preserve the supplied single full-frame dock geography, ships, soldiers, ropes, chains, fog, and dawn light. Soldiers run along the dock, haul one rope, and pull one iron pin while the nearest ship rocks. End during active coordinated work; no camera change, inserted view, split screen, repeated panel, or visible words.",
        "audio_intent": "effects-ambience", "environment_sound": "river wind, rigging, and military dock activity", "effects": "boots, chain, rope strain, work calls",
        "performance": {"baseline": "urgent coordinated labor", "trigger": "the retreat order reaches the dock", "visible_response": "soldiers converge on ropes and pins", "suppression": "no heroic posing", "decision": "the release work continues into the cut"},
    },
    {
        "id": "fallback-cao-advisor-reaction-i2v", "title": "曹操谋士反应 I2V 回退",
        "topic": "An advisor sees fire ships emerging through fog", "role": "reaction",
        "character": {"id": "advisor", "name": "谋士", "identity": "Lean middle-aged Han-era strategist in a gray robe and black cap."},
        "image_prompt": "One vertical full-frame cinematic over-shoulder reaction on a lean middle-aged Han-era strategist in a gray robe and black cap at a foggy river command post. A dark red armored shoulder is soft foreground; tiny orange fire points appear far across the river. One physical camera view, no modern objects, words, borders, panels, collage, logos, or interface.",
        "video_prompt": "Preserve the supplied strategist, robe, cap, foreground shoulder, river fog, and single full-frame composition. He looks past the commander, notices orange fire points growing through the mist, and tightens his expression without speaking. End as a distant alarm begins; no camera change, inserted view, split screen, repeated panel, or visible words.",
        "audio_intent": "effects-ambience", "environment_sound": "river wind and command-post room tone", "effects": "a distant alarm gong begins",
        "performance": {"baseline": "focused calculation", "trigger": "fire points emerge in the fog", "visible_response": "eyes fix and jaw tightens", "suppression": "no gasp or head shake", "decision": "he turns attention toward the advancing threat"},
    },
    {
        "id": "fallback-fight-dodge-i2v", "title": "雨巷闪避 I2V 回退",
        "topic": "A courier narrowly dodges one staff strike", "role": "medium",
        "character": {"id": "fighter", "name": "护送者", "identity": "Original lean martial artist in a dark indigo coat with short tied hair and a wrapped wooden case."},
        "image_prompt": "One vertical full-frame cinematic medium side view in a narrow ancient rain alley. An original lean martial artist in a dark indigo coat protects a wrapped wooden case; one attacker's wooden staff enters from screen left just before impact. Wet stones and continuous alley geography, one physical camera view, no words, borders, panels, collage, logos, or interface.",
        "video_prompt": "Preserve the supplied fighter, indigo coat, wrapped case, attacker staff, rain alley, and single full-frame side composition. The staff crosses once from screen left; the courier pivots once and lets it miss by inches while keeping the case protected. End during the pivot; no camera change, inserted view, split screen, repeated panel, or visible words.",
        "audio_intent": "effects-ambience", "environment_sound": "hard rain and alley resonance", "effects": "staff whoosh, wet foot pivot, cloth movement",
        "performance": {"baseline": "balanced defensive stance", "trigger": "one staff strike enters", "visible_response": "eyes and shoulders lead one compact pivot", "suppression": "no acrobatics or victory pose", "decision": "he completes the dodge into the next cut"},
    },
    {
        "id": "fallback-fight-impact-i2v", "title": "雨巷撞击 I2V 回退",
        "topic": "A courier redirects one attacker into a wooden stall", "role": "medium",
        "character": {"id": "fighter", "name": "护送者", "identity": "Original lean martial artist in a dark indigo coat with short tied hair and a wrapped wooden case."},
        "image_prompt": "One vertical full-frame cinematic medium-wide side view in the same ancient rain alley. An original lean martial artist in a dark indigo coat keeps a wrapped wooden case strapped across his back while redirecting one attacker's forearm toward a fragile wooden stall. Capture the instant before impact, clear bodies and geography, one physical camera view, no words, borders, panels, collage, logos, or interface.",
        "video_prompt": "Preserve the supplied fighter, indigo coat, wrapped case, attacker, stall, rain alley, and single full-frame side composition. The courier completes one compact redirection; the attacker hits the stall once and loose boards burst outward while the courier continues past without posing. End during falling debris; no camera change, inserted view, split screen, repeated panel, slow-motion replay, or visible words.",
        "audio_intent": "effects-ambience", "environment_sound": "hard rain and alley resonance", "effects": "one forearm block, one wooden impact, boards cracking and falling",
        "performance": {"baseline": "compact defensive motion", "trigger": "the attacker overcommits", "visible_response": "the courier redirects the forearm into the stall", "suppression": "no repeated hit, acrobatics, or victory pose", "decision": "he moves through the impact into the next cut"},
    },
    {
        "id": "fallback-fight-escape-i2v", "title": "雨巷突围 I2V 回退",
        "topic": "The courier escapes through a side gate during falling debris", "role": "ending_hook",
        "character": {"id": "fighter", "name": "护送者", "identity": "Original lean martial artist in a dark indigo coat with short tied hair and a wrapped wooden case."},
        "image_prompt": "One vertical full-frame cinematic wide view of an ancient rain alley side gate. The indigo-coated courier with a wrapped wooden case is beginning a sprint through the gate while two attackers react beside a collapsing wooden stall. Clear geography, wet stones, one physical camera view, no words, borders, panels, collage, logos, or interface.",
        "video_prompt": "Preserve the supplied courier, wrapped case, attackers, gate, stall, rain, and single full-frame geography. Falling boards distract the attackers while the courier accelerates through the side gate. End during the sprint and collapse; no victory pose, camera change, inserted view, split screen, repeated panel, or visible words.",
        "audio_intent": "effects-ambience", "environment_sound": "hard rain and alley resonance", "effects": "running feet, boards falling, one impact",
        "performance": {"baseline": "compressed readiness", "trigger": "the stall begins collapsing", "visible_response": "the courier commits toward the gate", "suppression": "no celebration or pause", "decision": "he escapes during continuing motion"},
    },
    {
        "id": "fallback-dialogue-station-establishing-i2v", "title": "车站告别建立镜头 I2V 回退",
        "topic": "Two people face an approaching deadline at a rainy station", "role": "establishing",
        "characters": [
            {"id": "woman", "name": "女人", "identity": "Original woman in a navy coat holding one train ticket."},
            {"id": "man", "name": "男人", "identity": "Original man in a charcoal jacket carrying a small canvas bag."},
        ],
        "image_prompt": "One vertical full-frame cinematic medium-wide view on a blue-hour rural railway platform in drizzle. An original woman in a navy coat holds one ticket screen left; an original man in a charcoal jacket with a canvas bag stands several steps away screen right. A distant train headlight approaches. One physical camera view, no readable signs, words, borders, panels, collage, logos, or interface.",
        "video_prompt": "Preserve the supplied single full-frame platform, two people, clothes, ticket, bag, drizzle, and approaching headlight. The headlight grows slightly, the woman tightens the ticket, and the man shifts one step without speaking. Keep their distance and end during rain and train motion; no camera change, split screen, repeated panel, or visible words.",
        "audio_intent": "score-ambience", "environment_sound": "drizzle, empty platform, and a distant approaching train", "effects": "soft rail vibration",
        "performance": {"baseline": "restrained distance", "trigger": "the train light approaches", "visible_response": "she tightens the ticket and he shifts weight", "suppression": "no waving or farewell pose", "decision": "both hold the unresolved distance"},
    },
    {
        "id": "fallback-short-drama-establishing-i2v", "title": "钱包短剧建立镜头 I2V 回退",
        "topic": "A rain-soaked courier returns a lost wallet at a doorway", "role": "establishing",
        "characters": [
            {"id": "courier", "name": "外卖员", "identity": "Original young Chinese courier in an unbranded yellow rain jacket."},
            {"id": "resident", "name": "住户", "identity": "Original older Chinese man in a dark sweater."},
        ],
        "image_prompt": "One vertical full-frame cinematic medium-wide rainy apartment doorway at night. An original young Chinese courier in an unbranded yellow rain jacket stands outside holding a wet brown wallet; an older Chinese male resident in a dark sweater opens the door only partway. One physical camera view, natural hands, no readable branding, words, borders, panels, collage, logos, or interface.",
        "video_prompt": "Preserve the supplied single full-frame doorway, courier, resident, clothes, wallet, rain, and screen positions. The courier offers the wallet with both hands while catching breath; the resident keeps the door partly closed and looks at it. Neither speaks yet. End during the offer; no camera change, split screen, repeated panel, or visible words.",
        "audio_intent": "effects-ambience", "environment_sound": "rain outside and quiet apartment interior", "effects": "rain jacket movement and door hinge",
        "performance": {"baseline": "courteous caution", "trigger": "the door opens", "visible_response": "the courier lifts the wallet and the resident studies it", "suppression": "no broad gestures", "decision": "the offer continues into the cut"},
    },
    {
        "id": "fallback-short-drama-dialogue-i2v", "title": "钱包短剧对白 I2V 回退",
        "topic": "The courier explains why he returned a wet wallet", "role": "closeup",
        "character": {"id": "courier", "name": "外卖员", "identity": "Original young Chinese courier in an unbranded yellow rain jacket."},
        "image_prompt": "One vertical full-frame cinematic over-shoulder closeup at a rainy apartment doorway. An original young Chinese courier in an unbranded yellow rain jacket holds a wet brown wallet with both hands; only the resident's soft shoulder edge enters foreground. One physical camera view, natural face and hands, no readable branding, words, borders, panels, collage, logos, or interface.",
        "video_prompt": "Preserve the supplied courier, yellow rain jacket, wallet, doorway, rain, foreground shoulder, and single full-frame closeup. He offers the wallet, catches one breath, and speaks one earnest sentence. End while his hands still hold the wallet forward; no camera change, split screen, repeated panel, or visible words.",
        "dialogue": {"speaker": "courier", "text": "您落在楼下了，我怕里面的证件淋湿。", "emotion": "earnest and breathless"},
        "audio_intent": "dialogue", "environment_sound": "rain outside and quiet doorway room tone", "effects": "wet jacket and wallet movement",
        "performance": {"baseline": "earnest breathlessness", "trigger": "the resident studies him suspiciously", "visible_response": "he steadies the wallet and explains", "suppression": "no pleading gesture or exaggerated mouth motion", "decision": "he keeps offering the wallet"},
    },
    {
        "id": "fallback-upstream-captions-i2v", "title": "显式上游字幕 I2V 验收",
        "topic": "A presenter states one verification principle with upstream captions", "role": "closeup",
        "subtitle_source": "upstream",
        "character": {"id": "presenter", "name": "讲述者", "identity": "Original Chinese woman in a plain blue shirt."},
        "image_prompt": "One vertical full-frame clean medium closeup of an original Chinese woman in a plain blue shirt against a neutral studio wall. One physical camera view with empty lower-frame caption space, natural face, no existing text, borders, panels, collage, logos, or interface.",
        "video_prompt": "Preserve the supplied presenter, blue shirt, neutral wall, and single full-frame camera. She looks into lens and speaks one concise sentence naturally. Render only the requested provider-native Chinese caption near the lower safe area; no camera change, split screen, repeated panel, logo, or interface.",
        "dialogue": {"speaker": "presenter", "text": "今天的重点，是先确认事实。", "emotion": "clear and calm"},
        "audio_intent": "dialogue", "environment_sound": "quiet neutral studio room tone", "effects": "",
        "audio_notes": "Use provider-native synchronized speech and provider-native Chinese captions only; keep one full-frame composition.",
        "performance": {"baseline": "calm direct address", "trigger": "the recording begins", "visible_response": "one natural blink and concise delivery", "suppression": "no presenter gestures or head bobbing", "decision": "hold attentive eye contact after the sentence"},
    },
)

for news_index, news_case in enumerate(
    (
        ("swift-orbit", "NASA Swift observatory above Earth", "NASA's Swift observatory resumed science observations after recovery work.", "A realistic NASA Swift-like space observatory above Earth with solar arrays, one full-frame orbital view, no readable markings.", "The observatory drifts above Earth while sunlight moves across its solar arrays; preserve one full-frame orbital view.", "NASA 的 Swift 望远镜已恢复科学观测。"),
        ("instrument-check", "A clean-room instrument verification", "Teams verified the recovered science instruments before observations resumed.", "A realistic aerospace clean-room style instrument verification scene with engineers studying telemetry displays whose text is not readable, one full-frame view.", "Engineers compare one instrument response and confirm the stable signal with restrained motion; no inserted screens or readable interface.", "团队完成仪器检查后，才恢复观测。"),
        ("science-observation", "Swift resumes observing a distant transient", "Swift continues rapid observations of changing high-energy events.", "A realistic orbital telescope aimed toward a distant transient star field above Earth's dark limb, one cinematic full-frame view, no labels.", "The telescope slews slowly toward a distant transient while the star field and Earth limb remain physically continuous.", "它将继续追踪快速变化的高能天体现象。"),
        ("source-reminder", "A final evidence-focused orbital image", "The update is grounded in NASA's dated mission notice and should be checked against the source.", "A realistic orbital observatory crossing sunrise above Earth, balanced empty space for narration but no text, one full-frame cinematic view.", "The observatory crosses orbital sunrise as the camera holds one continuous full-frame view; end during motion.", "这条信息应以 NASA 发布的任务更新为准。"),
    ),
    1,
):
    suffix, topic, fact, image_prompt, video_prompt, narration = news_case
    I2V_FALLBACK_CASES += (
        {
            "id": f"fallback-news-{news_index:02d}-{suffix}-i2v", "title": f"新闻镜头 {news_index} I2V 回退",
            "topic": topic, "role": "establishing" if news_index == 1 else ("ending_hook" if news_index == 4 else "insert"),
            "workflow": "single-image-animation", "character": None,
            "image_prompt": "Vertical 9:16 portrait composition. " + image_prompt + " No words, captions, borders, panels, collage, logos, or interface.",
            "video_prompt": video_prompt + " No camera change, split screen, repeated panel, or visible words.",
            "narration": narration,
            "audio_intent": "narration", "environment_sound": "restrained orbital ambience and light documentary score", "effects": "",
            "audio_notes": f"Use provider-native narration in the audio channel only. Source-grounded fact: {fact} Render no on-screen words.",
            "performance": {"baseline": "steady factual observation", "trigger": fact, "visible_response": "one motivated physical or orbital change", "suppression": "no sensational graphics or fake interface", "decision": "continue the evidence-led visual into the cut"},
        },
    )


def prepare_i2v_fallback_cases(root: Path) -> list[dict[str, Any]]:
    reports: list[dict[str, Any]] = []
    for case in I2V_FALLBACK_CASES:
        project_root = root / str(case["id"])
        if not (project_root / "project.json").is_file():
            run_cli(
                "init", str(project_root), "--title", str(case["title"]), "--topic", str(case["topic"]),
                "--workflow", str(case.get("workflow", "single-image-animation")), "--mode", "image-to-video", "--shots", "1", "--seconds", "6",
                "--video-size", "720x1280", "--aspect-ratio", "9:16", "--video-resolution", "480p", "--video-provider", "quickai",
            )
        path = project_root / "project.json"
        project = json.loads(path.read_text(encoding="utf-8"))
        project["workflow"] = str(case.get("workflow", "single-image-animation"))
        project["story"] = str(case["topic"])
        project["style_bible"] = (
            "Feature-film naturalism, physically grounded motion, one uninterrupted camera-original image, "
            "no text, captions, logos, watermarks, app interface, borders, panels, collage, or repeated frames."
        )
        case_characters = case.get("characters")
        if isinstance(case_characters, list):
            project["characters"] = [dict(item) for item in case_characters if isinstance(item, dict)]
        else:
            project["characters"] = [dict(case["character"])] if isinstance(case.get("character"), dict) else []
        project["audio"]["subtitle_source"] = str(case.get("subtitle_source", "none"))
        project["director"].update({"project_type": "single-clip", "mode": "single-shot", "strict": False})
        project["character_master"].update({"enabled": False, "generate": False})
        shot = project["shots"][0]
        dialogue_value = case.get("dialogue")
        dialogue = []
        if isinstance(dialogue_value, dict):
            dialogue = [{
                "id": "line-001", "speaker": str(dialogue_value["speaker"]), "text": str(dialogue_value["text"]),
                "emotion": str(dialogue_value["emotion"]), "start": 0.6, "end": 4.4,
            }]
        shot.update(
            {
                "summary": str(case["topic"]), "shot_role": str(case["role"]),
                "character_ids": list(case.get("character_ids", [str(item["id"]) for item in project["characters"]])),
                "generate_image": True, "use_character_master": False,
                "image_prompt": str(case["image_prompt"]), "video_prompt": str(case["video_prompt"]),
                "video_references": [], "dialogue": dialogue,
                "narration": str(case.get("narration", "")),
                "audio_intent": str(case["audio_intent"]), "environment_sound": str(case["environment_sound"]),
                "sound_effects": str(case.get("effects", "")),
                "audio_notes": str(
                    case.get(
                        "audio_notes",
                        "Use provider-native synchronized audio; render no on-screen words."
                        if project["audio"]["subtitle_source"] == "none"
                        else "Use provider-native synchronized audio and provider-native captions only.",
                    )
                ),
                "performance": dict(case["performance"]), "camera": "one stable single full-frame composition",
                "camera_motion": "restrained motivated movement", "exit_action": "visible motion continues through the edit point",
                "exit_behavior": "ending-hook" if case["role"] == "ending_hook" else "cut-on-action",
                "edit_in": 0.2, "edit_out": 5.6, "timeline_duration": 5.4,
            }
        )
        path.write_text(json.dumps(project, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        validation = run_cli("validate", str(project_root))
        reports.append({"id": case["id"], "provider": "quickai", "project": str(project_root), "valid": validation["ok"]})
    return reports


def prepare_i2v_cases(root: Path, source: Path) -> list[dict[str, Any]]:
    if not source.is_file():
        raise FileNotFoundError(f"I2V source does not exist: {source}")
    reports: list[dict[str, Any]] = []
    for provider in ("quickai", "quickainew"):
        case_id = f"{provider}-i2v-portrait-reaction"
        project_root = root / case_id
        if not (project_root / "project.json").is_file():
            run_cli(
                "init", str(project_root), "--title", f"{provider} I2V portrait reaction", "--topic", "Animate one approved keyframe",
                "--workflow", "single-image-animation", "--mode", "image-to-video", "--shots", "1", "--seconds", "6",
                "--video-size", "720x1280", "--aspect-ratio", "9:16", "--video-resolution", "480p", "--video-provider", provider,
            )
        destination = project_root / "assets" / "references" / "portrait-source.jpg"
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.resolve() != destination.resolve():
            shutil.copy2(source, destination)
        path = project_root / "project.json"
        project = json.loads(path.read_text(encoding="utf-8"))
        project["story"] = "The approved portrait becomes one restrained reaction without changing identity, clothing, room, or composition."
        shot = project["shots"][0]
        shot.update(
            {
                "summary": "She notices the unseen listener start to leave.",
                "shot_role": "medium",
                "generate_image": False,
                "image_prompt": "",
                "video_references": ["assets/references/portrait-source.jpg"],
                "video_prompt": "Preserve the exact woman, dark green blouse, natural face, apartment light, background, and portrait composition. The unseen listener begins to leave; her eyes track slightly off-camera, she draws a small breath, and turns her head only a few degrees as if deciding whether to speak. No scene change and no visible words.",
                "audio_intent": "score-ambience",
                "environment_sound": "quiet apartment room tone and distant city traffic",
                "audio_notes": "restrained unresolved score, no speech",
                "performance": {"baseline": "restrained hurt", "trigger": "the unseen listener starts to leave", "visible_response": "small eye shift and breath", "suppression": "she remains composed", "decision": "she turns slightly but does not speak"},
                "exit_behavior": "continue-action",
                "edit_in": 0.2,
                "edit_out": 5.4,
                "timeline_duration": 5.2,
            }
        )
        path.write_text(json.dumps(project, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        validation = run_cli("validate", str(project_root))
        reports.append({"id": case_id, "provider": provider, "project": str(project_root), "valid": validation["ok"]})
    return reports


def prepare_full_case(root: Path, case: dict[str, Any], i2v_source: Path | None) -> dict[str, Any]:
    project_root = root / str(case["id"])
    workflow = str(case["workflow"])
    mode = str(case.get("mode", "text-to-video"))
    provider = str(case.get("provider", "quickai"))
    generation_seconds = int(case.get("generation_seconds", 6))
    if not (project_root / "project.json").is_file():
        arguments = [
            "init", str(project_root), "--title", str(case["title"]), "--topic", str(case["story"]),
            "--workflow", workflow, "--mode", mode, "--shots", str(len(case["shots"])), "--seconds", str(generation_seconds),
            "--video-size", "720x1280", "--aspect-ratio", "9:16", "--video-resolution", "480p", "--video-provider", provider,
        ]
        for genre in case.get("genre", []):
            arguments.extend(["--genre", str(genre)])
        if case.get("subtitle_source"):
            arguments.extend(["--subtitle-source", str(case["subtitle_source"])])
        run_cli(*arguments)
    path = project_root / "project.json"
    project = json.loads(path.read_text(encoding="utf-8"))
    project["story"] = str(case["story"])
    project["story_beats"] = []
    project["characters"] = list(case.get("characters", []))
    project["character_bible"] = " | ".join(
        f"{item['name']}: {item['identity']}" for item in project["characters"] if isinstance(item, dict)
    )
    project["style_bible"] = (
        "Feature-film naturalism, physically grounded motion, clear spatial continuity, "
        "edge-to-edge photographed physical space, one uninterrupted camera-original image."
    )
    project["target_duration_seconds"] = round(sum(float(item["edit_duration"]) for item in case["shots"]), 3)
    project["audio"]["mode"] = "native-dialogue"
    project["audio"]["generate_audio"] = True
    project["audio"]["subtitle_source"] = str(case.get("subtitle_source", "none"))
    if mode == "image-to-video":
        if i2v_source is None or not i2v_source.is_file():
            raise FileNotFoundError(f"{case['id']} requires --i2v-source")
        destination = project_root / "assets" / "references" / "approved-source.jpg"
        destination.parent.mkdir(parents=True, exist_ok=True)
        if i2v_source.resolve() != destination.resolve():
            shutil.copy2(i2v_source, destination)
        project["character_master"].update({"enabled": False, "generate": False})
    character_ids = [str(item["id"]) for item in project["characters"] if isinstance(item, dict)]
    for index, (shot, specification) in enumerate(zip(project["shots"], case["shots"]), 1):
        beat_id = f"beat-{index:03d}"
        project["story_beats"].append(
            {"id": beat_id, "role": str(specification["role"]), "visible_event": str(specification["visible_event"])}
        )
        edit_duration = float(specification["edit_duration"])
        edit_in = 0.0 if edit_duration >= generation_seconds else 0.2
        edit_out = edit_in + edit_duration
        dialogue_value = specification.get("dialogue")
        dialogue = []
        if isinstance(dialogue_value, dict):
            dialogue = [
                {
                    "id": f"line-{index:03d}",
                    "speaker": str(dialogue_value["speaker"]),
                    "text": str(dialogue_value["text"]),
                    "emotion": str(dialogue_value["emotion"]),
                    "start": 0.2,
                    "end": min(float(generation_seconds) - 0.2, 3.4),
                }
            ]
        shot.update(
            {
                "summary": str(specification["visible_event"]),
                "beat_id": beat_id,
                "shot_role": str(specification["role"]),
                "character_ids": list(specification.get("character_ids", character_ids)),
                "generate_image": False,
                "use_character_master": False,
                "image_prompt": "",
                "video_prompt": str(specification["prompt"]),
                "video_references": ["assets/references/approved-source.jpg"] if mode == "image-to-video" else [],
                "dialogue": dialogue,
                "audio_intent": str(specification["audio_intent"]),
                "environment_sound": "natural location ambience matched to the visible scene",
                "sound_effects": str(specification.get("effects", "")),
                "audio_notes": "Use provider-native synchronized audio; no speech unless dialogue is declared.",
                "camera": "composition appropriate to the declared shot role",
                "camera_motion": "restrained motivated movement",
                "entry_action": "the visible event is already beginning",
                "exit_action": "motivated motion continues through the edit point",
                "exit_behavior": "ending-hook" if specification["role"] == "ending_hook" else "cut-on-action",
                "performance": {
                    "baseline": "natural task-focused behavior",
                    "trigger": str(specification["visible_event"]),
                    "visible_response": "a specific readable physical or facial response",
                    "suppression": "avoid exaggerated head, mouth, and farewell movement",
                    "decision": "continue the motivated action into the cut",
                },
                "seconds": generation_seconds,
                "edit_in": round(edit_in, 3),
                "edit_out": round(edit_out, 3),
                "timeline_duration": round(edit_duration, 3),
            }
        )
    path.write_text(json.dumps(project, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    validation = run_cli("validate", str(project_root))
    return {
        "id": case["id"],
        "workflow": workflow,
        "provider": provider,
        "project": str(project_root),
        "shots": len(case["shots"]),
        "timeline_seconds": project["target_duration_seconds"],
        "valid": validation["ok"],
    }


def prepare_sourced_news(root: Path) -> dict[str, Any]:
    project_root = root / "module-sourced-news-20s"
    news_path = project_root / "news.json"
    project_path = project_root / "project.json"
    news = json.loads(news_path.read_text(encoding="utf-8"))
    checked_at = "2026-08-30T10:45:00+08:00"
    news.update(
        {
            "as_of": checked_at,
            "selection": {
                "mode": "hot-topic-research",
                "window_hours": 72,
                "rationale": "NASA 于 8 月 28 日更新 Swift 科学观测状态，属于 72 小时内的一手任务更新。",
                "search_queries": [
                    "Swift observatory restarts science observations August 28 2026 LINK rendezvous",
                    "NASA Swift resumed two science instruments August 26 2026 news",
                ],
            },
            "sources": [
                {
                    "id": "nasa-swift-restart",
                    "title": "NASA's Swift Restarts Science Observations",
                    "publisher": "NASA Science",
                    "url": "https://science.nasa.gov/blogs/swift/2026/08/28/nasas-swift-restarts-science-observations/",
                    "published_at": "2026-08-28T15:04:00-04:00",
                    "accessed_at": checked_at,
                    "source_type": "primary",
                    "visual_rights": "facts-only",
                },
                {
                    "id": "ap-swift-rescue",
                    "title": "Rescue mission is called off for NASA's aging Swift space telescope",
                    "publisher": "Associated Press",
                    "url": "https://apnews.com/article/800bfbe5aaba5fec9aadd20bdcc138a5",
                    "published_at": "2026-08-19T19:07:35Z",
                    "accessed_at": checked_at,
                    "source_type": "secondary",
                    "visual_rights": "facts-only",
                },
            ],
            "claims": [
                {"id": "claim-001", "text": "Swift 于 2026 年 8 月 26 日恢复紫外/光学和 X 射线两台仪器运行。", "source_ids": ["nasa-swift-restart"]},
                {"id": "claim-002", "text": "爆发警报望远镜仍未恢复，团队计划在未来几周尝试恢复数据采集。", "source_ids": ["nasa-swift-restart"]},
                {"id": "claim-003", "text": "LINK 不再抓取和抬升 Swift，但仍计划演示交会与近距离操作。", "source_ids": ["nasa-swift-restart", "ap-swift-rescue"]},
                {"id": "claim-004", "text": "NASA 预计 Swift 将在未来一至两个月降至约 300 公里高度。", "source_ids": ["nasa-swift-restart"]},
            ],
            "script_segments": [
                {"shot_id": "shot-001", "narration": "NASA 的 Swift 望远镜，刚刚恢复两台科学仪器。", "claim_ids": ["claim-001"]},
                {"shot_id": "shot-002", "narration": "紫外光学和 X 射线观测已重启。", "claim_ids": ["claim-001"]},
                {"shot_id": "shot-003", "narration": "第三台爆发警报仪器，仍等待恢复。", "claim_ids": ["claim-002"]},
                {"shot_id": "shot-004", "narration": "抬升取消后，交会测试继续，留给 Swift 的时间可能只有一两个月。", "claim_ids": ["claim-003", "claim-004"]},
            ],
            "editorial": {"status": "verified", "fact_checked_at": checked_at, "unresolved_conflicts": [], "corrections": []},
        }
    )
    news_path.write_text(json.dumps(news, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    project = json.loads(project_path.read_text(encoding="utf-8"))
    project["story"] = "Swift 恢复两台仪器，但抬升计划取消；科学观测和在轨服务演示进入新的倒计时。"
    project["style_bible"] = "Clearly illustrative scientific visualization, physically plausible orbital motion, never presented as authentic event footage."
    roles = ["establishing", "wide", "insert", "reaction"]
    visible = ["以说明性轨道画面建立 Swift 与地球", "两台仪器重新开启", "第三台仪器保持关闭", "LINK 改为交会测试且 Swift 轨道继续下降"]
    prompts = [
        "Clearly illustrative scientific visualization of an original Swift-like observatory orbiting above Earth, full vertical frame, restrained camera drift, physically plausible solar panels and orbital motion.",
        "Explanatory cutaway visualization of the observatory as two optical instrument housings power on in sequence; use light and moving mechanisms rather than readable labels or interface graphics.",
        "Macro explanatory view of a third detector remaining physically closed while the other two continue tracking a distant stellar burst; no dashboard or screen graphics.",
        "Wide orbital visualization: a small servicing craft performs a careful distant approach without docking while the observatory continues along a visibly lower orbit above Earth's limb.",
    ]
    project["story_beats"] = []
    for index, shot in enumerate(project["shots"]):
        beat_id = f"beat-{index + 1:03d}"
        project["story_beats"].append({"id": beat_id, "role": roles[index], "visible_event": visible[index]})
        shot.update(
            {
                "summary": visible[index], "beat_id": beat_id, "shot_role": roles[index],
                "video_prompt": prompts[index], "audio_intent": "narration",
                "environment_sound": "subtle scientific documentary ambience", "audio_notes": "provider-native Mandarin narration with restrained score",
                "camera": "clear explanatory composition", "camera_motion": "restrained motivated drift",
                "entry_action": "the visual explanation is already in progress", "exit_action": "orbital or mechanical motion continues into the cut",
                "exit_behavior": "cut-on-action", "edit_in": 0.0, "edit_out": 5.0, "timeline_duration": 5.0,
                "performance": {"baseline": "clear scientific explanation", "trigger": visible[index], "visible_response": "one observable change", "suppression": "no sensational motion", "decision": "continue the explanation into the cut"},
            }
        )
        if index == 2:
            # Paid acceptance exposed a provider-made black leader on this
            # explanatory insert. Reuse the good task and trim the bad head.
            shot.update({"edit_in": 1.3, "edit_out": 5.0, "timeline_duration": 3.7})
    project_path.write_text(json.dumps(project, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report = run_cli("news-validate", str(project_root))
    return {"id": project_root.name, "project": str(project_root), "valid": report["ok"]}


def prepare_series_episode_one(root: Path) -> dict[str, Any]:
    series_root = root / "module-episodic-series"
    series_path = series_root / "series.json"
    series = json.loads(series_path.read_text(encoding="utf-8"))
    series.update(
        {
            "season_arc": "林澜发现一把不属于自己的钥匙，追踪到楼上空置公寓，最终发现有人仍在暗中使用那里。",
            "season_theme": "在熟悉空间中辨认被忽略的异常",
            "conflict_escalation": "陌生钥匙出现，楼上空屋留下新鲜痕迹。",
            "midpoint": "钥匙真的能打开空屋。",
            "climax": "屋内门后传来第二个人的脚步。",
            "ending_hook": "林澜转身时，门从外面缓缓合上。",
            "style_bible": "Feature-film suspense naturalism, rainy practical light, consistent green blouse and brass key, uninterrupted camera-original imagery.",
            "locations": [{"id": "home", "name": "林澜公寓"}, {"id": "empty-flat", "name": "楼上空置公寓"}],
            "props": [{"id": "brass-key", "name": "带蓝线的旧黄铜钥匙"}],
            "characters": [{"id": "lin-lan", "name": "林澜", "identity": "Original Chinese woman in her early thirties, tied black hair, dark green blouse and charcoal trousers."}],
        }
    )
    series["episodes"][0].update(
        {
            "title": "桌下的钥匙", "synopsis": "雨夜停电后，林澜在餐桌下发现一把陌生钥匙，并听见楼上传来拖动声。",
            "continuity_in": "林澜独自在家，穿深绿色衬衫；屋外大雨。",
            "intended_continuity_out": "林澜握着带蓝线的黄铜钥匙站在楼梯口，楼上再次传来拖动声。",
            "character_states": {"lin-lan": "警觉但克制，手持陌生钥匙"},
        }
    )
    series["episodes"][1].update(
        {
            "title": "空屋的门", "synopsis": "林澜用钥匙打开楼上空屋，发现一杯仍温热的水。",
            "continuity_in": "", "intended_continuity_out": "", "character_states": {},
        }
    )
    series_path.write_text(json.dumps(series, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    run_cli("series-sync", str(series_root))

    project_root = series_root / "episodes" / "ep-001"
    project_path = project_root / "project.json"
    project = json.loads(project_path.read_text(encoding="utf-8"))
    project["story"] = series["episodes"][0]["synopsis"]
    project["story_beats"] = [
        {"id": "beat-001", "role": "establishing", "visible_event": "停电的雨夜公寓建立空间"},
        {"id": "beat-002", "role": "insert", "visible_event": "桌下露出带蓝线的黄铜钥匙"},
        {"id": "beat-003", "role": "reaction", "visible_event": "林澜听见楼上拖动声并走向楼梯"},
    ]
    prompts = [
        "Wide rainy apartment at night during a brief power outage. Original woman Lin Lan in a dark green blouse crosses the dining room with a small flashlight while window rain moves behind her.",
        "Tight insert beneath the dining table: the flashlight beam reveals one old brass key tied with blue thread among moving rain reflections; her hand stops just before picking it up.",
        "Close reaction on the same woman holding the brass key. A heavy dragging sound comes from upstairs; her eyes move toward the ceiling, then she starts toward the dark stairwell. End during her first step.",
    ]
    roles = ["establishing", "insert", "reaction"]
    intents = ["score-ambience", "effects-ambience", "effects-ambience"]
    for index, shot in enumerate(project["shots"]):
        shot.update(
            {
                "summary": project["story_beats"][index]["visible_event"], "beat_id": f"beat-{index + 1:03d}", "shot_role": roles[index],
                "character_ids": ["lin-lan"], "video_prompt": prompts[index], "audio_intent": intents[index],
                "environment_sound": "steady rain and apartment room tone", "sound_effects": "flashlight handling, distant floor creak and dragging only when visible or motivated",
                "camera": "cinematic suspense coverage", "camera_motion": "restrained motivated movement",
                "entry_action": "the action is already beginning", "exit_action": "motion or attention continues into the cut", "exit_behavior": "cut-on-action",
                "edit_in": 0.0, "edit_out": 5.0, "timeline_duration": 5.0,
                "performance": {"baseline": "task-focused caution", "trigger": project["story_beats"][index]["visible_event"], "visible_response": "small eye or hand response", "suppression": "no theatrical fear", "decision": "move toward the source"},
            }
        )
    project_path.write_text(json.dumps(project, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report = run_cli("series-preflight", str(series_root), "--episode", "ep-001")
    return {"id": series_root.name, "project": str(series_root), "episode": "ep-001", "valid": report["ok"]}


def prepare_series_episode_two(root: Path) -> dict[str, Any]:
    series_root = root / "module-episodic-series"
    project_root = series_root / "episodes" / "ep-002"
    project_path = project_root / "project.json"
    project = json.loads(project_path.read_text(encoding="utf-8"))
    context = project.get("series_context") if isinstance(project.get("series_context"), dict) else {}
    accepted = str(context.get("previous_episode_continuity", "")).strip()
    if not accepted:
        raise RuntimeError("ep-002 cannot be prepared before ep-001 has an accepted continuity summary")
    project["story"] = "林澜沿楼梯找到空置公寓，用黄铜钥匙开门，并在空屋里发现一杯仍温热的水。"
    project["story_beats"] = [
        {"id": "beat-001", "role": "establishing", "visible_event": "林澜从楼梯口接近空置公寓"},
        {"id": "beat-002", "role": "insert", "visible_event": "已验收的黄铜钥匙打开旧门锁"},
        {"id": "beat-003", "role": "reaction", "visible_event": "她发现温热水杯并听见身后门响"},
    ]
    prompts = [
        "Continue exactly from accepted episode one: same original Lin Lan, dark green blouse, charcoal trousers, flashlight and old brass key, same rainy night. Wide stair landing as she reaches the unlit empty-flat door.",
        "Tight insert preserving the accepted old brass key in her hand. The key enters a worn lock, turns once, and the door opens a narrow gap while flashlight light moves across peeling paint.",
        "Close reaction on the same Lin Lan inside the empty flat. She sees steam still rising from one plain cup, then the entry door begins moving behind her; she turns during the motion, no theatrical gasp or final pose.",
    ]
    intents = ["score-ambience", "effects-ambience", "effects-ambience"]
    for index, shot in enumerate(project["shots"]):
        shot.update(
            {
                "summary": project["story_beats"][index]["visible_event"], "beat_id": f"beat-{index + 1:03d}",
                "shot_role": project["story_beats"][index]["role"], "character_ids": ["lin-lan"],
                "continuity_notes": accepted, "video_prompt": prompts[index], "audio_intent": intents[index],
                "environment_sound": "the same steady rain, stairwell room tone, and quiet empty-flat resonance",
                "sound_effects": "footsteps, one key turn, old door hinge, cup and room sounds only when motivated",
                "camera": "cinematic suspense coverage preserving screen direction", "camera_motion": "restrained motivated movement",
                "entry_action": "continue directly from the accepted prior state", "exit_action": "attention or object motion continues into the cut",
                "exit_behavior": "cut-on-action", "edit_in": 0.0, "edit_out": 5.0, "timeline_duration": 5.0,
                "performance": {"baseline": "controlled caution", "trigger": project["story_beats"][index]["visible_event"], "visible_response": "small eye, hand, or breath response", "suppression": "no theatrical fear", "decision": "investigate the next physical clue"},
            }
        )
    project_path.write_text(json.dumps(project, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report = run_cli("series-preflight", str(series_root), "--episode", "ep-002")
    return {"id": series_root.name, "project": str(series_root), "episode": "ep-002", "valid": report["ok"], "inherits": accepted}


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare v2.1 paid A/B acceptance projects without creating provider tasks.")
    parser.add_argument("output", type=Path)
    parser.add_argument("--prepare-i2v-source", action="store_true")
    parser.add_argument("--i2v-source", type=Path)
    parser.add_argument("--full", action="store_true", help="Prepare module-specific and six cross-module acceptance projects.")
    parser.add_argument("--i2v-fallbacks", action="store_true", help="Prepare paid single-frame I2V fallbacks for rejected T2V shots.")
    parser.add_argument("--orchestration", action="store_true", help="Prepare sourced-news and first-episode series acceptance contracts.")
    parser.add_argument("--series-episode-two", action="store_true", help="Prepare episode two only after episode one's accepted continuity exists.")
    args = parser.parse_args()
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=True)
    raw_by_id = {str(item["id"]): item for item in AB_CASES}
    report = [prepare_case(root, resolved_case(item, raw_by_id)) for item in AB_CASES]
    report.append(prepare_layout_i2v_case(root))
    source_project = prepare_i2v_source(root) if args.prepare_i2v_source else None
    if args.i2v_source:
        report.extend(prepare_i2v_cases(root, args.i2v_source.resolve()))
    fallback_report = prepare_i2v_fallback_cases(root / "full") if args.i2v_fallbacks else []
    full_report = []
    if args.full:
        selected_source = args.i2v_source.resolve() if args.i2v_source else None
        full_report = [prepare_full_case(root / "full", case, selected_source) for case in FULL_CASES]
    orchestration_report = []
    if args.orchestration:
        orchestration_report = [prepare_sourced_news(root / "full"), prepare_series_episode_one(root / "full")]
    if args.series_episode_two:
        orchestration_report.append(prepare_series_episode_two(root / "full"))
    print(
        json.dumps(
            {
                "ok": all(item["valid"] for item in [*report, *fallback_report, *full_report, *orchestration_report]),
                "projects": report,
                "i2v_fallback_projects": fallback_report,
                "full_projects": full_report,
                "orchestration_projects": orchestration_report,
                "i2v_source_project": str(source_project) if source_project else "",
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
