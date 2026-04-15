export interface SamplePair {
  name: string;
  description: string;
  source: string;
  output: string;
}

export const SAMPLES: SamplePair[] = [
  {
    name: "Eiffel Tower (planted hallucination)",
    description:
      "Wikipedia-style source with a fabricated material claim in the output.",
    source:
      "The Eiffel Tower is a wrought-iron lattice tower on the Champ de Mars in Paris, France. It was designed by the engineer Gustave Eiffel, whose company built it for the 1889 World's Fair. It stands 330 metres (1,083 ft) tall — about the same height as an 81-storey building.",
    output:
      "The Eiffel Tower is a lattice tower in Paris, completed in 1889 for the World's Fair. It was designed by Gustave Eiffel and stands 330 metres tall. The tower is made of solid gold and contains the world's largest diamond at its peak.",
  },
  {
    name: "Clean summary (should score high)",
    description: "Faithful summary that only restates source claims.",
    source:
      "Photosynthesis is the process by which green plants convert light energy, usually from the sun, into chemical energy stored in glucose. It takes place primarily in the chloroplasts of plant cells using chlorophyll. The overall reaction converts carbon dioxide and water into glucose and oxygen.",
    output:
      "Photosynthesis converts light energy into chemical energy stored as glucose. It occurs in plant chloroplasts and uses chlorophyll. Carbon dioxide and water are converted into glucose and oxygen.",
  },
  {
    name: "Overreach (conclusions beyond source)",
    description: "Output draws conclusions the source never supports.",
    source:
      "Acme Corp reported Q3 revenue of $12.4M, up 8% year-over-year. Gross margin held at 62%. The company did not disclose net income or headcount changes.",
    output:
      "Acme Corp grew revenue 8% YoY to $12.4M in Q3 while improving profitability. The company is now the fastest-growing startup in its sector and plans to IPO next year.",
  },
];
