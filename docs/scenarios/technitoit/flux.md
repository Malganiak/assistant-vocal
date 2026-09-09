flowchart TD
  node_01k815ejvwe04b9wxkh463geq7["Subagent: Validation pour devis"]
  node_01k81240vbfq9scajwqppzqjga["Subagent: Identification de la demande"]
  node_01k8164cgpe04b9wz4c7hk2yag["Subagent: Validation pour suivi chantier"]
  node_01k83bgzkyen8arv7zgaa9cgfj["Subagent: Collecte des informations personnelles (adresse)"]
  node_01k814hs1ae04b9wwww0der8ev["Subagent: Collecte des informations personnelles (confirmation)"]
  node_01k815t1p9e04b9wy3f2caht3v["Subagent: Conclusion"]
  start_node(["Start"])
  node_01k83cfz44en8arvagenxamnry{{"Tool: tool_3001k811mehmfxrsrqb90t2a3rap"}}
  node_01k83c03yken8arv992910r3ns["Subagent: Collecte des informations personnelles (téléphone)"]
  node_01k813cfyxfq9scakdm784nc0e["Subagent: Qualification du projet (confirmation)"]
  node_01k8165r3se04b9wzvpja71d0a["Subagent: Validation pour SAV"]
  node_01k83cpseben8arvb8xth7t3g8(["End"])
  node_01k83b2n6fen8arv60a2d4gv47["Subagent: Qualification du projet (non propriétaire)"]
  node_01k83cw2q2en8arvbw0bd7yeky{{"Tool: tool_2401k836fb2we5f86y85p1ygm109"}}
  node_01k83b6qhxen8arv6pen04pbhm["Subagent: Qualification du projet (délai)"]
  node_01k83bpd07en8arv8kkrw466w5["Subagent: Collecte des informations personnelles (nom et prénom)"]
  node_01k83ayry0en8arv5g2wm8k3ys["Subagent: Qualification du projet (propriétaire)"]
  node_01k8164cgpe04b9wz4c7hk2yag -->|"Si l'utilisateur a donné un créneau pour être rappelé"| node_01k83cw2q2en8arvbw0bd7yeky
  node_01k83c03yken8arv992910r3ns -->|"Si l'utilisateur veut une demande autre qu'un devis ou une …"| node_01k8165r3se04b9wzvpja71d0a
  node_01k83c03yken8arv992910r3ns -->|"Si l'utilisateur veut un devis et a donné son numéro de tél…"| node_01k815ejvwe04b9wxkh463geq7
  node_01k815ejvwe04b9wxkh463geq7 -->|"Si l'utilisateur a donné un créneau pour être rappelé"| node_01k83cfz44en8arvagenxamnry
  node_01k83bgzkyen8arv7zgaa9cgfj -->|"Si l'utilisateur a donné son adresse postale (au minimum le…"| node_01k83c03yken8arv992910r3ns
  node_01k83cw2q2en8arvbw0bd7yeky --> node_01k815t1p9e04b9wy3f2caht3v
  node_01k83cfz44en8arvagenxamnry --> node_01k815t1p9e04b9wy3f2caht3v
  node_01k83bpd07en8arv8kkrw466w5 -->|"Si l'utilisateur a donné son nom de famille"| node_01k83bgzkyen8arv7zgaa9cgfj
  node_01k815t1p9e04b9wy3f2caht3v -->|"Si l'agent vocal ElevenLabs a dit au revoir à l'utilisateur"| node_01k83cpseben8arvb8xth7t3g8
  start_node --> node_01k81240vbfq9scajwqppzqjga
  node_01k83ayry0en8arv5g2wm8k3ys -->|"Si l'utilisateur n'est pas propriétaire"| node_01k83b2n6fen8arv60a2d4gv47
  node_01k83b6qhxen8arv6pen04pbhm -->|"Si l'utilisateur a donné un délai des travaux souhaités"| node_01k814hs1ae04b9wwww0der8ev
  node_01k81240vbfq9scajwqppzqjga -->|"Si l'utilisateur veut un devis"| node_01k813cfyxfq9scakdm784nc0e
  node_01k814hs1ae04b9wwww0der8ev -->|"Si l'utilisateur accepte de donner ses coordonnées"| node_01k83bpd07en8arv8kkrw466w5
  node_01k813cfyxfq9scakdm784nc0e -->|"Si l'utilisateur a répondu"| node_01k83ayry0en8arv5g2wm8k3ys
  node_01k83ayry0en8arv5g2wm8k3ys -->|"Si l'utilisateur est propriétaire"| node_01k83b6qhxen8arv6pen04pbhm
  node_01k83b2n6fen8arv60a2d4gv47 -->|"Si l'utilisateur est d'accord pour transmettre sa demande à…"| node_01k83b6qhxen8arv6pen04pbhm
  node_01k83c03yken8arv992910r3ns -->|"Si l'utilisateur veut un suivi de chantier et a donné son n…"| node_01k8164cgpe04b9wz4c7hk2yag
  node_01k81240vbfq9scajwqppzqjga -->|"Si l'utilisateur veut un suivi de chantier ou une autre dem…"| node_01k814hs1ae04b9wwww0der8ev
  node_01k8165r3se04b9wzvpja71d0a -->|"Si l'utilisateur a donné un créneau pour être rappelé"| node_01k83cw2q2en8arvbw0bd7yeky