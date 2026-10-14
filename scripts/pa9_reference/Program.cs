// Reference/data-export tool for the public PKHeX commit named below.
// SPDX-License-Identifier: AGPL-3.0-only
using PKHeX.Core;
using System.Text.Json;

const string Commit = "e15d2467b32da7bc26ce7cc8e5c4ede32740e20a";
if (args.Length != 2) throw new ArgumentException("export NEW_JSON | verify INPUT_DIRECTORY");
var options = new JsonSerializerOptions { WriteIndented = false };
if (args[0] == "export")
{
    var personal = new SortedDictionary<string, object>(StringComparer.Ordinal);
    int[] languages = [1,2,3,4,5,7,8,9,10,11];
    var table = PersonalTable.ZA;
    for (ushort species = 1; species <= table.MaxSpeciesID; species++)
    {
        for (byte form = 0; form < Math.Max((byte)1, table[species].FormCount); form++)
        {
            if (!table.IsPresentInGame(species, form)) continue;
            var pi = table[species, form];
            var (learn, plus) = LearnSource9ZA.GetLearnsetAndPlus(species, form);
            var names = languages.ToDictionary(l => l.ToString(), l => SpeciesName.GetSpeciesNameGeneration(species, l, 9));
            var lm = learn.GetAllMoves().ToArray(); var ll = learn.GetAllLevels().ToArray();
            var pm = plus.GetAllMoves().ToArray(); var pl = plus.GetAllLevels().ToArray();
            personal[$"{species}:{form}"] = new {
                species, form, internal_id = SpeciesConverter.GetInternal9(species), names,
                stats = new[] { pi.HP,pi.ATK,pi.DEF,pi.SPA,pi.SPD,pi.SPE },
                growth = pi.EXPGrowth, gender = pi.Gender, friendship = pi.BaseFriendship,
                abilities = new[] {pi.Ability1,pi.Ability2,pi.AbilityH},
                battle_only = FormInfo.IsBattleOnlyForm(species,form,9),
                learn = lm.Select((m,i) => new int[] {ll[i],m}).ToArray(),
                plus = pm.Select((m,i) => new int[] {pl[i],m}).ToArray(),
                tm = PersonalInfo9ZA.MachineMoves.ToArray().Where((m,i) => pi.GetIsLearnTM(i)).ToArray(),
            };
        }
    }
    var result = new { schema = 1, source_commit = Commit,
        source_license = "GPL-3.0-or-later", stat_order = "hp,atk,def,spa,spd,spe",
        growth = Enumerable.Range(0,6).Select(g => Experience.GetTable((byte)g).ToArray()).ToArray(),
        pp = MoveInfo.GetPPTable(EntityContext.Gen9a).ToArray().Select(x => (int)x).ToArray(),
        plus_moves = PersonalInfo9ZA.PlusMoves.ToArray(), personal };
    using var f = new FileStream(args[1],FileMode.CreateNew);
    JsonSerializer.Serialize(f,result,options);
}
else if (args[0] == "vectors")
{
    var vectors = new List<object>(); int n = 0;
    var table = PersonalTable.ZA;
    for (ushort species = 1; species <= table.MaxSpeciesID; species++)
    for (byte form = 0; form < Math.Max((byte)1,table[species].FormCount); form++)
    {
        if (!table.IsPresentInGame(species,form) || FormInfo.IsBattleOnlyForm(species,form,9)) continue;
        var pk = new PA9 { Species=species, Form=form, CurrentLevel=(byte)(new[] {1,5,50,100}[n%4]),
            StatAlignment=(Nature)(n%25), HyperTrainFlags=(byte)(n%64),
            IV_HP=n%32, IV_ATK=(n+1)%32, IV_DEF=(n+2)%32, IV_SPA=(n+3)%32, IV_SPD=(n+4)%32, IV_SPE=(n+5)%32,
            EV_HP=252, EV_ATK=252, EV_SPA=4 };
        pk.ResetPartyStats();
        vectors.Add(new {species,form,internal_id=pk.SpeciesInternal,level=pk.CurrentLevel,exp=pk.EXP,
            nature=(int)pk.StatAlignment,hyper=pk.HyperTrainFlags, ivs=new[]{pk.IV_HP,pk.IV_ATK,pk.IV_DEF,pk.IV_SPA,pk.IV_SPD,pk.IV_SPE},
            evs=new[]{pk.EV_HP,pk.EV_ATK,pk.EV_DEF,pk.EV_SPA,pk.EV_SPD,pk.EV_SPE},
            stats=new[]{pk.Stat_HPMax,pk.Stat_ATK,pk.Stat_DEF,pk.Stat_SPA,pk.Stat_SPD,pk.Stat_SPE} });
        n++;
    }
    using var f = new FileStream(args[1],FileMode.CreateNew);
    JsonSerializer.Serialize(f,new {source_commit=Commit,vectors},options);
}
else if (args[0] == "verify")
{
    var results = new List<object>();
    foreach (var file in Directory.GetFiles(args[1],"*.pa9").Order())
    {
        var raw = File.ReadAllBytes(file); var pk = new PA9(raw.ToArray());
        var before = new int[] {pk.Stat_HPMax,pk.Stat_ATK,pk.Stat_DEF,pk.Stat_SPA,pk.Stat_SPD,pk.Stat_SPE};
        var clone = pk.Clone(); clone.ResetPartyStats();
        var expected = new int[] {clone.Stat_HPMax,clone.Stat_ATK,clone.Stat_DEF,clone.Stat_SPA,clone.Stat_SPD,clone.Stat_SPE};
        var la = new LegalityAnalysis(pk);
        results.Add(new { file = Path.GetFileName(file), size = raw.Length,
            structural = pk.Valid, level = pk.CurrentLevel, cached_level = pk.Stat_Level,
            stats = before, expected_stats = expected, stats_match = before.SequenceEqual(expected) && pk.Stat_Level==clone.Stat_Level,
            legality = la.Valid, report = la.Report() });
    }
    Console.WriteLine(JsonSerializer.Serialize(results,options));
}
else throw new ArgumentException("Unknown mode");
