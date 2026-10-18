// Проверка формирования интерфейса и обработчиков без браузера.
import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
const root=path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const fixtures=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));
const elements=new Map(),events={},stored=new Map();
const element=k=>{if(!elements.has(k))elements.set(k,{innerHTML:'',open:false,showModal(){this.open=true},close(){this.open=false},querySelectorAll(){return[]},setAttribute(){}});return elements.get(k)};
const context=vm.createContext({
 document:{documentElement:{dataset:{}},querySelector:element,querySelectorAll:()=>[],addEventListener:(name,fn)=>events[name]=fn},
 window:{matchMedia:()=>({matches:false})},localStorage:{getItem:k=>stored.get(k),setItem:(k,v)=>stored.set(k,v)},
 fetch:async()=>({ok:false,status:401,json:async()=>({error:'Войдите в систему'})}),
 setTimeout:()=>0,clearTimeout:()=>{},console,URL,Blob,Date,
});
vm.runInContext(fs.readFileSync(path.join(root,'static/app.js'),'utf8'),context);
await new Promise(resolve=>setImmediate(resolve));
assert(element('#app').innerHTML.includes('auth-form'));
vm.runInContext("authMode='register';renderAuth()",context);
assert(element('#app').innerHTML.includes('Код приглашения'));
for(const role of ['admin','member']){
 context.fixture=fixtures[role];
 vm.runInContext('state=fixture;user=state.user;render()',context);
 const expected=role==='admin'?['overview','plan','map','facts','team','requests']:['overview','plan','team'];
 for(const page of expected){
  await events.click({target:{closest:()=>({dataset:{page},closest:()=>null})}});
  const html=element('#app').innerHTML;
  assert(html.length>500,page);
  assert(!html.includes('undefined'),page+' undefined text');
 }
 if(role==='member'){
  const html=element('#app').innerHTML;
  assert(!html.includes('data-page="map"'));
  assert(!html.includes('data-page="facts"'));
  assert(!html.includes('data-action="new-employee"'));
 }
}
context.fixture=fixtures.admin;
vm.runInContext("state=fixture;user=state.user;page='map';render()",context);
assert(element('#app').innerHTML.includes('star-map'));
for(const kind of ['transfer','exchange','help']){
 const html=vm.runInContext(`proposalKind='${kind}';proposalForm()`,context);
 assert(html.includes('Отправить на согласование'));assert(!html.includes('undefined'));
}
for(const id of ['ONK-2914','MOB-7011']){
 vm.runInContext(`taskDialog('${id}')`,context);
 assert(element('#drawer').innerHTML.includes('Почему получился такой план'));
}
vm.runInContext("taskDialog('ONK-2914')",context);assert(element('#drawer').innerHTML.includes('task-form'));
vm.runInContext("taskDialog('MOB-7012')",context);assert(!element('#drawer').innerHTML.includes('task-form'));
vm.runInContext("personDialog('ENG-401')",context);assert(element('#drawer').innerHTML.includes('Компетенции'));
vm.runInContext('newEmployeeDialog()',context);assert(element('#drawer').innerHTML.includes('employee-form'));
vm.runInContext('newTaskDialog()',context);assert(element('#drawer').innerHTML.includes('new-task-form'));
vm.runInContext('toggleTheme()',context);assert.equal(context.document.documentElement.dataset.theme,'dark');
vm.runInContext('toggleTheme()',context);assert.equal(context.document.documentElement.dataset.theme,'light');
vm.runInContext("page='plan';render()",context);
await events.click({target:{closest:()=>({dataset:{page:'overview'},closest:()=>null})}});
assert.equal(vm.runInContext('page',context),'overview');
// Движение узла: координаты, линии и сохранение положения.
vm.runInContext("page='map';render()",context);
const id=vm.runInContext('graphNodes[0].id',context),before=vm.runInContext('({...graphNodes[0]})',context);
const transforms={};
const group={dataset:{node:id},setAttribute:(k,v)=>transforms[k]=v};
const svg={createSVGPoint:()=>({x:0,y:0,matrixTransform(){return{x:this.x,y:this.y}}}),getScreenCTM:()=>({inverse:()=>({})}),setPointerCapture(){},releasePointerCapture(){},hasPointerCapture:()=>true,classList:{add(){},remove(){}},querySelectorAll:selector=>selector==='[data-node]'?[group]:[],setAttribute(){}};
elements.set('#star-map',svg);
const target={closest:s=>s==='#star-map'?svg:s==='[data-node]'?group:null};
events.pointerdown({target,button:0,pointerId:1,clientX:10,clientY:10,preventDefault(){}});
events.pointermove({pointerId:1,clientX:80,clientY:50});
events.pointerup({pointerId:1});
const after=vm.runInContext('graphNodes[0]',context);
assert.equal(after.x,before.x+70);assert.equal(after.y,before.y+40);
assert(transforms.transform.includes('translate('));assert(stored.has('orbita-map-'+fixtures.admin.project_id));
assert(!vm.runInContext("esc('<img src=x onerror=alert(1)>')",context).includes('<img'));
assert(!vm.runInContext('factsView()',context).includes('Загрузить JSON'));
// Назначенный вручную сотрудник виден даже без доступного фрагмента плана.
context.manualTask=JSON.parse(JSON.stringify(fixtures.admin.tasks.find(t=>t.team==='Team-L'&&t.status!=='Done')));
Object.assign(context.manualTask,{id:'BLOCK-UI',manual_assignments:{'Разработчик Java':'ENG-401'},history:[],facts:[]});
context.manualTask.entry.fragments=[];
vm.runInContext('state.tasks.push(manualTask)',context);
const manualRow=vm.runInContext('taskTable([manualTask],false)',context);
assert(manualRow.includes(vm.runInContext("esc(personName('ENG-401'))",context)));
assert(!manualRow.includes('Не назначены'));
vm.runInContext("personDialog('ENG-401')",context);
assert(element('#drawer').innerHTML.includes('BLOCK-UI'));
// История плана и подтверждённые 6 часов факта отображаются раздельно.
context.closedTask=JSON.parse(JSON.stringify(context.manualTask));
Object.assign(context.closedTask,{id:'HIST-UI',status:'Done',remaining_sp:0,remaining_hours:{},manual_assignments:{},history:[{task_id:'HIST-UI',team:'Team-L',sprint:1,sp:1,assignments:[{engineer:'ENG-401',role:'Разработчик Java',hours:4}]}],facts:[{sprint:1,status:'Done',remaining_sp:0,remaining_hours:{},spent_hours:{'Разработчик Java':6}}]});
Object.assign(context.closedTask.entry,{status:'done',end:1});
vm.runInContext('state.tasks.push(closedTask);state.cutoff=1',context);
assert(vm.runInContext('taskTable([closedTask])',context).includes('Архив плана'));
vm.runInContext("taskDialog('HIST-UI')",context);
assert(element('#drawer').innerHTML.includes('Подтверждённый факт по спринтам'));
assert(element('#drawer').innerHTML.includes('>6</td>'));
assert(element('#drawer').innerHTML.includes('1 · архив плана'));
console.log('PASS: role pages, forms, themes, home, graph drag/persistence, manual assignees, archived plan, confirmed actuals, escaping.');

// Заголовки и русское склонение, включая 11–14 и 21/33/111.
assert(!vm.runInContext("head('<img src=x onerror=alert(1)>')",context).includes('<img'));
for(const [n,phrase] of [[0,'задач требуют'],[1,'задача требует'],[2,'задачи требуют'],[4,'задачи требуют'],[5,'задач требуют'],[11,'задач требуют'],[14,'задач требуют'],[20,'задач требуют'],[21,'задача требует'],[33,'задачи требуют'],[111,'задач требуют']]){
 assert.equal(vm.runInContext(`plural(${n},'задача требует','задачи требуют','задач требуют')`,context),phrase);
}
// Риски, разбор плана и звёздная карта ядра.
context.fixture=fixtures.admin;
vm.runInContext("state=fixture;user=state.user;page='overview';render()",context);
for(const text of ['Риски плана','Разбор плана','Подробный разбор','Жадный план','Зачтено по факту'])assert(element('#app').innerHTML.includes(text),text);
assert(element('#app').innerHTML.includes('badge orange'));
assert(element('#app').innerHTML.includes('data-action="scenarios"'));
vm.runInContext("page='map';mapMode='risks';render()",context);
for(const text of ['Вся организация','Уникальные профили','BF = 1'])assert(element('#app').innerHTML.includes(text),text);
const explained=vm.runInContext(`explainMarkup({tasks:[{id:'T',status:'postponed',reason:'<b>x</b> Если включить',counterfactual:{status:'feasible',lost:[],gained:[]}}],initiatives:[]})`,context);
assert(explained.includes('Если включить')&&!explained.includes('<b>x</b>'));
console.log('PASS: core risks, plan analysis, explain and organization star map.');
// Роль выбирается из списка; новая роль, совпавшая по ключу, становится существующей.
context.fixture=fixtures.admin;
vm.runInContext("state=fixture;user=state.user;page='team';render();newEmployeeDialog()",context);
assert(element('#drawer').innerHTML.includes('data-role-select'));
assert(element('#drawer').innerHTML.includes('+ Новая роль'));
assert(element('#drawer').innerHTML.includes('>Девопс</option>'));
context.row=(value,typed='')=>({querySelector:selector=>selector==='[data-role-select]'?{value}:{value:typed}});
assert.equal(vm.runInContext("pickedRole(row('__new','дев_опс'))",context),'Девопс');
assert.equal(vm.runInContext("pickedRole(row('__new','Разработчик-JAVA'))",context),'Разработчик Java');
assert.equal(vm.runInContext("pickedRole(row('__new','Новая   роль'))",context),'Новая роль');
assert.equal(vm.runInContext("pickedRole(row('Аналитик'))",context),'Аналитик');
vm.runInContext("page='plan';render()",context);
assert(element('#app').innerHTML.includes('Что означают коды'));
assert(element('#app').innerHTML.includes('Табельный номер')||element('#app').innerHTML.includes('Номер задачи'));
console.log('PASS: role picker, role spelling unification and code glossary.');
// Данные спринта: карточка загрузки датасета и итогового пересчёта.
vm.runInContext("state=fixture;user=state.user;page='facts';render()",context);
assert(element('#app').innerHTML.includes('Данные спринта 1'));
assert(element('#app').innerHTML.includes('Отчёты команд об итогах спринта'));
vm.runInContext("user=Object.assign({},state.user,{org_replan:true});render()",context);
assert(element('#app').innerHTML.includes('Итоговый пересчёт спринта 1'));
assert(element('#app').innerHTML.includes('Загрузить обновлённый датасет'));
console.log('PASS: sprint data upload and final recalculation card.');
// Отсутствия: форма в карточке сотрудника и сводка в разделе команды.
context.fixture=fixtures.admin;
vm.runInContext("state=fixture;user=state.user;page='team';render();personDialog(state.people.find(p=>p.allocations[user.team]>0).id)",context);
assert(element('#drawer').innerHTML.includes('Отметить отпуск, болезнь или обучение'));
assert(element('#drawer').innerHTML.includes('data-action="absence-preview"'));
assert(element('#app').innerHTML.includes('Отсутствия в команде'));
vm.runInContext("state.absences=[{id:'a1',engineer:state.people[0].id,start:'2026-10-05',end:'2026-10-09',reason:'Болезнь',comment:'<b>x</b>',lost:{'1':40}}];render()",context);
assert(element('#app').innerHTML.includes('−40 ч'));
assert(!element('#app').innerHTML.includes('<b>x</b>'));
console.log('PASS: employee absence form and team absences.');
// Пользовательский текст не превращается в HTML в основных представлениях.
context.fixture=JSON.parse(JSON.stringify(fixtures.admin));
const hostile='<img src=x onerror=alert(1)>';
Object.assign(context.fixture.user,{name:hostile});
Object.assign(context.fixture.people[0],{name:hostile,roles:[hostile],skills:[hostile]});
Object.assign(context.fixture.tasks[0],{title:hostile,initiative:hostile});
vm.runInContext('state=fixture;user=state.user',context);
for(const render of ['overview()','planView()','teamView()','mapView()']){
 assert(!vm.runInContext(render,context).includes(hostile),render);
}
console.log('PASS: escaped headings/user text and Russian risk counts.');

// До базы — сбор голосов, а не форма факта или преждевременная фиксация.
context.fixture=JSON.parse(JSON.stringify(fixtures.admin));
context.fixture.baseline=false;
context.fixture.baseline_approval={required:6,confirmed:1,own_approved:true,stale:false,heads:[{team:'Team-L',name:'Глава',approved:true},{team:'Team-K',name:'Глава',approved:false}]};
vm.runInContext('state=fixture;user=state.user',context);
for(const render of ['overview()','factsView()']){
 const html=vm.runInContext(render,context);
 assert(html.includes('Подтверждения: 1 из 6'));
 assert(html.includes('Ваше согласие учтено'));
 assert(!html.includes('data-action="approve"'));
 assert(!html.includes('id="fact-form"'));
}
vm.runInContext('state.baseline_approval.stale=true;state.baseline_approval.own_approved=false;state.baseline_approval.confirmed=0',context);
assert(vm.runInContext('factsView()',context).includes('Требуется согласие заново'));
assert(vm.runInContext('overview()',context).includes('data-action="approve"'));
console.log('PASS: baseline approval progress and facts gate.');

// Начальная карта воспроизводима; круги и области подписей не пересекаются.
context.fixture=JSON.parse(JSON.stringify(fixtures.admin));
stored.delete('orbita-map-'+fixtures.admin.project_id);
vm.runInContext('state=fixture;user=state.user;graphMarkup()',context);
const initialNodes=JSON.parse(vm.runInContext('JSON.stringify(graphNodes)',context));
const boxes=JSON.parse(vm.runInContext('JSON.stringify(graphNodes.map(n=>graphBounds(n)))',context));
for(let i=0;i<boxes.length;i++){
 const a=boxes[i];
 assert(a.left>=0&&a.right<=1000&&a.top>=0&&a.bottom<=650,'map bounds '+initialNodes[i].id);
 for(let j=0;j<i;j++){
  const b=boxes[j];
  assert(a.right<=b.left||a.left>=b.right||a.bottom<=b.top||a.top>=b.bottom,'overlap '+initialNodes[i].id+' / '+initialNodes[j].id);
 }
}
vm.runInContext('graphMarkup()',context);
assert.deepEqual(JSON.parse(vm.runInContext('JSON.stringify(graphNodes)',context)),initialNodes);
stored.set('orbita-map-'+fixtures.admin.project_id,JSON.stringify({'p:ENG-405':{x:410,y:330}}));
vm.runInContext('graphMarkup()',context);
assert.equal(vm.runInContext("graphNodes.find(n=>n.id==='p:ENG-405').x",context),410);
assert.equal(vm.runInContext("graphNodes.find(n=>n.id==='p:ENG-405').y",context),330);
console.log('PASS: initial map spacing, deterministic layout and saved positions.');

// Отсутствующее в Excel имя не превращаем в выдуманное ФИО.
context.fixture=JSON.parse(JSON.stringify(fixtures.admin));
vm.runInContext('state=fixture;user=state.user',context);
assert.equal(vm.runInContext("personLabel({id:'ENG-000',name:'ENG-000',registered:false,roles:['Разработчик Java']})",context),'ENG-000 · Разработчик Java');
assert.equal(vm.runInContext("personLabel({id:'ENG-000',name:'ENG-000',registered:false,roles:[]})",context),'ENG-000 · Роль не указана');
assert.equal(vm.runInContext("personLabel({id:'ENG-000',name:'Имя из аккаунта',registered:true,roles:['Разработчик Java']})",context),'Имя из аккаунта');
assert.equal(vm.runInContext("personLabel({id:'ENG-000',name:'Имя нового сотрудника',registered:false,roles:['Разработчик Java']})",context),'Имя нового сотрудника');
const unnamed=context.fixture.people.find(p=>p.name===p.id&&!p.registered&&p.allocations['Team-L']>0);
context.unnamedId=unnamed.id;
const fallback=vm.runInContext('esc(personName(unnamedId))',context);
assert(vm.runInContext('teamView()',context).includes(fallback));
vm.runInContext('personDialog(unnamedId)',context);
assert(element('#drawer').innerHTML.includes(fallback));
const map=vm.runInContext('graphMarkup()',context);
assert(map.includes('class="person-role"'));
assert(map.includes(vm.runInContext('esc(state.people.find(p=>p.id===unnamedId).roles.join(", "))',context)));
// Имя из users.name используется и на карте, полный текст остаётся в title/aria.
vm.runInContext("state.people[0].name='Имя из аккаунта';state.people[0].registered=true",context);
assert(vm.runInContext('graphMarkup()',context).includes('Имя из аккаунта'));
console.log('PASS: employee ID/role fallback, real names and map labels.');

// Роли и Bus Factor, профиль команды, проверка отсутствия, стресс-сценарии и группировка изменений.
context.fixture=JSON.parse(JSON.stringify(fixtures.admin));
vm.runInContext("state=fixture;user=state.user;page='map';mapMode='risks';render()",context);
for(const text of ['Роли: кто закрывает работы','Как считается Bus Factor','data-whatif','Девопс'])assert(element('#app').innerHTML.includes(text),text);
vm.runInContext("page='map';mapMode='graph';render()",context);
assert(element('#app').innerHTML.includes('Единственный носитель роли'));
assert(vm.runInContext('graphMarkup()',context).includes('critical'));
vm.runInContext("teamDialog('Team-Platform')",context);
assert(element('#drawer').innerHTML.includes('Покрытие ролей'));
assert(!element('#drawer').innerHTML.includes('undefined'));
vm.runInContext("whatIfDialog('ENG-419')",context);
assert(element('#drawer').innerHTML.includes('whatif-form'));
assert(element('#drawer').innerHTML.includes('Единственный носитель роли'));
vm.runInContext("personDialog('ENG-419')",context);
assert(element('#drawer').innerHTML.includes('data-whatif="ENG-419"'));
const whatIf=vm.runInContext(`whatIfMarkup({engineer_id:'ENG-419',from_sprint:1,to_sprint:2,lost:{'1':80,'2':80},goals_lost:['PRODF-7120'],goals_kept:14,goals_total:15,initiatives:[{id:'PRODF-7120',end:8,reason:'<b>x</b>'}],changes:[],risks:[]})`,context);
assert(whatIf.includes('14 из 15')&&whatIf.includes('PRODF-7120')&&!whatIf.includes('<b>x</b>'));
const scenarios=vm.runInContext(`scenarioMarkup({base_done:15,scenarios:[{key:'capacity',title:'Ёмкость',note:'n',initiatives_done:13,initiatives_total:15,tasks_included:31,tasks_total:37,lost:['PRODF-7120'],gained:[],initiatives:[{id:'PRODF-7120',rung:74,forecast:8,reason:'r'}],tasks:[{id:'KP-0000',status:'postponed',decision:'postpone',forecast:7,reason:'<i>y</i>'}]}]})`,context);
assert(scenarios.includes('13 из 15')&&scenarios.includes('Перенести на следующий квартал')&&!scenarios.includes('<i>y</i>'));
const grouped=vm.runInContext(`changesMarkup([{task_id:'A',kind:'schedule',before:{start:1,end:2},after:{start:2,end:3,status:'planned'},reason:'r1'},{task_id:'B',kind:'done',before:{end:1},after:{end:1,status:'done'},reason:'r2'},{task_id:'C',kind:'assignments',before:{start:2,end:3},after:{start:2,end:3,status:'planned'},reason:'r3'},{task_id:'D',kind:'schedule',before:{start:5,end:5},after:{start:3,end:5,status:'planned'},reason:'r4'}])`,context);
assert(grouped.includes('Закрыты по факту (1)')&&grouped.includes('изменились исполнители')&&grouped.includes('начало С5 → С3'));
// Итоги спринта: у отклонения видны его последствия.
vm.runInContext("state.reviews={'1':{sprint:1,deviations:[{task_id:'SRV-4011',team:'Team-Alpha',text:'не завершена'}],consequences:{'SRV-4011':[{task_id:'SRV-4010',before:3,after:5}]},goals_lost:[],goals_gained:[],capacity:[]}}",context);
assert(vm.runInContext('reviewsMarkup()',context).includes('Повлекло'));
// Escape закрывает карточку.
vm.runInContext("personDialog('ENG-419')",context);
events.keydown({key:'Escape',preventDefault(){},target:{closest:()=>null}});
assert.equal(element('#drawer').open,false);
console.log('PASS: role bus factor, team profile, what-if, stress scenarios, grouped changes and sprint consequences.');
