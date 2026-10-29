import sys
# 模拟企业数据库
employees = {"1001": "张三 (研发部)", "1002": "李四 (产品部)"}
emp_id = sys.argv[1] if len(sys.argv) > 1 else "1001"
print(f"查询结果：工号 {emp_id} 的员工是 {employees.get(emp_id, '未知员工')}")