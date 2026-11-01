import pandas as pd, numpy as np, json, re, os
import matplotlib; matplotlib.use('Agg'); import matplotlib.pyplot as plt
os.chdir(os.path.expanduser('~/mnt/tenderlens'))
OUT='reports/crispdm/'; S={}
C1='#1F4E79'; C2='#E07A2F'; C3='#7A8B99'; C4='#2E8B57'
plt.rcParams.update({'font.size':11,'axes.spines.top':False,'axes.spines.right':False,'figure.dpi':160,'axes.titleweight':'bold'})
def save(n): plt.tight_layout(); plt.savefig(OUT+n+'.png'); plt.close()
t=pd.read_csv('data/tenders.csv',low_memory=False,dtype=str)
S['n_notices']=len(t); S['n_cols']=t.shape[1]
cat=t['procurementCategory-categorieApprovisionnement'].fillna('').str.replace('*','',regex=False).str.strip()
def lab(x):
    x=x.upper()
    k=[w for w,c in [('GOODS','GD'),('SERVICES','SRV'),('CONSTRUCTION','CNST'),('SRVTGD','SRVTGD')] if c in x.split('\n') or c in re.split(r'[\s,]+',x)]
    return '+'.join(k) if k else (x or 'vide')
cat2=cat.apply(lambda x: ', '.join(sorted(set(re.split(r'[\s,*]+',x))-{''})) or 'vide')
S['category']=cat2.value_counts().head(8).to_dict()
nt=t['noticeType-avisType-eng'].fillna('vide'); S['notice_type']=nt.value_counts().to_dict()
pm=t['procurementMethod-methodeApprovisionnement-eng'].fillna('vide'); S['method']=pm.value_counts().to_dict()
st=t['tenderStatus-appelOffresStatut-eng'].fillna('vide'); S['status']=st.value_counts().to_dict()
pub=pd.to_datetime(t['publicationDate-datePublication'],errors='coerce'); clo=pd.to_datetime(t['tenderClosingDate-appelOffresDateCloture'],errors='coerce',utc=True).dt.tz_localize(None)
S['pub_min']=str(pub.min().date()); S['pub_max']=str(pub.max().date())
# missing
key={'Titre':'title-titre-eng','Date publication':'publicationDate-datePublication','Date clôture':'tenderClosingDate-appelOffresDateCloture','UNSPSC':'unspsc','Catégorie':'procurementCategory-categorieApprovisionnement','Méthode':'procurementMethod-methodeApprovisionnement-eng','Raison appel limité':'limitedTenderingReason-raisonAppelOffresLimite-eng','Accords commerciaux':'tradeAgreements-accordsCommerciaux-eng','Acheteur':'contractingEntityName-nomEntitContractante-eng','Pièces jointes':'attachment-piecesJointes-eng','Description':'tenderDescription-descriptionAppelOffres-eng','Début contrat prévu':'expectedContractStartDate-dateDebutContratPrevue'}
miss={}
for k,c in key.items():
    v=t[c].fillna('').str.strip()
    miss[k]=round(100*((v=='')|(v.str.lower()=='none')).mean(),1)
S['missing']=miss
ltr=t['limitedTenderingReason-raisonAppelOffresLimite-eng'].fillna('').str.strip()
S['ltr_literal_none']=round(100*(ltr.str.lower()=='none').mean(),1); S['ltr_empty']=round(100*(ltr=='').mean(),1)
m=pd.Series(miss).sort_values()
plt.figure(figsize=(8,5)); plt.barh(m.index,m.values,color=[C2 if x>30 else C1 for x in m.values]); plt.xlabel('% vide ou "None"'); plt.title('Valeurs manquantes — avis d\'appel d\'offres'); 
for i,x in enumerate(m.values): plt.text(x+0.5,i,f'{x}%',va='center',fontsize=9)
save('fig_missing')
# monthly
mo=pub.dt.to_period('M').value_counts().sort_index()
plt.figure(figsize=(9,4)); plt.plot(mo.index.to_timestamp(),mo.values,color=C1,lw=2); plt.title('Avis publiés par mois (CanadaBuys)'); plt.ylabel('avis'); save('fig_monthly')
S['monthly_mean']=int(mo.mean())
# category & method bars
fig,ax=plt.subplots(1,2,figsize=(11,4.2))
cc=cat2.value_counts().head(6); ax[0].barh(cc.index[::-1],cc.values[::-1],color=C1); ax[0].set_title('Catégorie d\'approvisionnement')
mm=pm.value_counts().head(6); ax[1].barh([s[:38] for s in mm.index[::-1]],mm.values[::-1],color=C2); ax[1].set_title('Méthode d\'approvisionnement')
save('fig_cat_method')
# window
w=(clo-pub).dt.total_seconds()/86400
S['window_valid']=int(w.notna().sum()); S['window_neg']=int((w<0).sum()); S['window_gt365']=int((w>365).sum())
S['window_median']=round(float(w[(w>=0)&(w<=365)].median()),1)
S['window_q']= {q:round(float(w[(w>=0)&(w<=365)].quantile(q)),1) for q in [0.05,0.25,0.5,0.75,0.95]}
ww=w[(w>=0)&(w<=120)]
plt.figure(figsize=(8,4)); plt.hist(ww,bins=60,color=C1); plt.axvline(S['window_median'],color=C2,ls='--',label=f"médiane {S['window_median']} j"); plt.legend(); plt.xlabel('jours entre publication et clôture'); plt.title('Délai de soumission (0–120 j)'); save('fig_window')
# window by method
top=pm.value_counts().head(4).index
d=[w[(pm==x)&(w>=0)&(w<=120)].dropna() for x in top]
plt.figure(figsize=(8,4)); plt.boxplot(d,tick_labels=[x[:22] for x in top],showfliers=False,patch_artist=True,boxprops=dict(facecolor='#cfe0f0')); plt.ylabel('jours'); plt.title('Délai de soumission par méthode'); save('fig_window_method')
S['window_by_method']={x:round(float(v.median()),1) for x,v in zip(top,d)}
# unspsc4
u=t['unspsc'].fillna('').apply(lambda s: next((p for p in re.split(r'[^0-9]+',s) if len(p)>=4),''))
u4=u.str[:4]; S['unspsc_missing']=round(100*(u4=='').mean(),1)
sel=json.load(open('reports/selected_unspsc4.json'))
desc=t.assign(u4=u4).groupby('u4')['unspscDescription-eng'].agg(lambda s:s.dropna().iloc[0] if s.notna().any() else '')
sc=u4[u4.isin(sel)].value_counts()
S['selected_counts']=sc.to_dict()
plt.figure(figsize=(8,6)); lbl=[f"{k}" for k in sc.index[::-1]]; plt.barh(lbl,sc.values[::-1],color=[C2 if k in('2510','4110') else C1 for k in sc.index[::-1]]); plt.title('Les 20 catégories UNSPSC-4 retenues (avis)'); plt.xlabel('avis'); save('fig_unspsc')
# attachments
att=t['attachment-piecesJointes-eng'].fillna('')
S['with_cb_attachment']=int(att.str.contains('canadabuys.canada.ca').sum())
# 2510
m2510=u4=='2510'; S['n_2510']=int(m2510.sum())
u8=u[m2510].str[:8].value_counts(); S['n_2510_u8']=int(u8.size); S['generic_2510']=round(100*u[m2510].str[:8].str.endswith('0000').mean(),1)
# awards
a=pd.read_csv('data/awards.csv',dtype=str,engine='python',on_bad_lines='skip')
S['n_awards']=len(a)
sup=a['supplierLegalName-nomLegalFournisseur-eng'].fillna('').str.upper().str.strip(); S['n_suppliers']=int(sup.nunique())
val=pd.to_numeric(a['totalContractValue-valeurTotaleContrat'],errors='coerce'); S['award_value_missing']=round(100*val.isna().mean(),1); S['award_value_median']=float(val.median()); S['award_value_neg_or_zero']=int((val<=0).sum())
vv=val[val>0]
plt.figure(figsize=(8,4)); plt.hist(np.log10(vv),bins=60,color=C4); plt.xlabel('log10(valeur totale du contrat, CAD)'); plt.title('Valeur des contrats attribués (échelle log)'); save('fig_award_value')
S['award_q']={q:float(vv.quantile(q)) for q in [0.25,0.5,0.75,0.99]}
q1,q3=np.log10(vv).quantile([.25,.75]); iqr=q3-q1; S['award_outliers_log_iqr']=int(((np.log10(vv)<q1-1.5*iqr)|(np.log10(vv)>q3+1.5*iqr)).sum())
sc2=sup[sup!=''].value_counts(); S['top10_supplier_share']=round(100*sc2.head(10).sum()/sc2.sum(),1)
# census
c=pd.read_csv('reports/census_2510.csv',low_memory=False)
S['census_rows']=len(c); S['census_tenders']=int(c.reference.nunique()); S['census_labels']=int(c.label.nunique())
cm=c.comparator.fillna('none').value_counts(normalize=True).mul(100).round(1); S['comparators']=cm.to_dict()
dm=c.dimension.fillna('none').value_counts().head(10); S['dimensions']=dm.to_dict()
fig,ax=plt.subplots(1,2,figsize=(11,4.2))
ax[0].bar(cm.index,cm.values,color=C1); ax[0].set_title('Comparateurs (recensement 2510)'); ax[0].set_ylabel('%')
ax[1].barh(dm.index[::-1],dm.values[::-1],color=C2); ax[1].set_title('Dimensions physiques')
save('fig_census')
opt=c.groupby('reference').size(); S['obs_per_tender_median']=float(opt.median()); S['obs_per_tender_max']=int(opt.max())
lt=c.groupby('label').reference.nunique(); S['labels_single_tender_pct']=round(100*(lt==1).mean(),1)
try:
  cmap=pd.read_csv('reports/census_2510_mapped.csv',low_memory=False); S['mapped_cols']=cmap.columns.tolist()[:40]; S['mapped_rows']=len(cmap)
except Exception as e: S['mapped_err']=str(e)
# results
for g,f in [('2510','reports/kb_t13/2510/register.csv'),('4110','reports/kb_ab/4110/register.csv')]:
    r=pd.read_csv(f); last=r.groupby('reference').tail(1); S['res_'+g]={'status':last.status.value_counts().to_dict(),'pile':last.pile.value_counts().to_dict()}
json.dump(S,open(OUT+'stats.json','w'),indent=1,default=str)
print(json.dumps(S,indent=1,default=str))
